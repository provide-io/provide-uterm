#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Reading a scenario, and refusing one that would mean nothing.

A scenario is the contract every language is held to, so a malformed one is
refused at load. The failure mode this guards against is not a crash — it is
sixteen cells quietly agreeing about the wrong thing, which reads exactly like
parity.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import jsonschema
from harness.expectations import ANY_REPETITION, Expectation, parse_expectation

_HERE: Final = Path(__file__).resolve().parent
#: Where the committed scenarios live.
SCENARIO_DIR: Final = _HERE.parent / "scenarios"
#: Where the schemas both sides of the protocol are validated against live.
SCHEMA_DIR: Final = _HERE.parent / "schema"

#: Actions that address a session and are meaningless without one.
_NEEDS_SESSION: Final = frozenset({"get_session", "session_snapshot", "session_events", "set_input_mode"})
#: Actions that name a path themselves rather than deriving one.
_NEEDS_PATH: Final = frozenset({"http_get", "http_post"})
#: Actions that act on a worker's lease.
_NEEDS_WORKER: Final = frozenset(
    {
        "hijack_acquire",
        "hijack_heartbeat",
        "hijack_send",
        "hijack_step",
        "hijack_snapshot",
        "hijack_release",
    }
)
#: Actions that act on a lease somebody already holds.
_NEEDS_LEASE: Final = _NEEDS_WORKER - {"hijack_acquire"}
#: A reference to what an earlier step observed, resolved by the driver.
_REFERENCE: Final = re.compile(r"^\$\{([a-z0-9_]+)\.([A-Za-z0-9_.]+)\}$")


@dataclass(frozen=True)
class Step:
    """One thing a driver does, and how its observations are compared."""

    id: str
    action: str
    auth: str
    path: str | None
    session_id: str | None
    body: Any
    volatile: tuple[str, ...]
    worker_id: str | None = None
    hijack_id: str | None = None
    owner: str | None = None
    lease_s: int | None = None
    keys: str | None = None
    input_mode: str | None = None
    limit: int | None = None
    repeat: int = 1

    @property
    def observation_ids(self) -> tuple[str, ...]:
        """The ids this step's observations are recorded under.

        A step that runs once keeps its own id — numbering those would rewrite
        every expectation already committed. A repeated step records one
        observation per repetition, because a scenario repeats a step
        precisely when it expects the answers to stop being the same.
        """
        if self.repeat == 1:
            return (self.id,)
        return tuple(f"{self.id}.{index}" for index in range(self.repeat))


@dataclass(frozen=True)
class Scenario:
    """A scenario as loaded, with its expectations already parsed."""

    id: str
    title: str
    timeout_ms: int
    requires: tuple[str, ...]
    auth: str
    mutates: bool
    steps: tuple[Step, ...]
    expectations: tuple[Expectation, ...]
    raw: Mapping[str, Any]
    path: Path

    @property
    def volatile_by_step(self) -> dict[str, tuple[str, ...]]:
        """The paths each step declares volatile, for the comparison.

        A repeated step declares them once and every repetition carries them:
        what makes a field volatile is the field, not which time round it was
        read.
        """
        return {observed: step.volatile for step in self.steps if step.volatile for observed in step.observation_ids}


def schema(name: str) -> dict[str, Any]:
    """One of the committed schemas, by file stem."""
    return json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text())


def load_scenario(path: Path) -> Scenario:
    """Read and validate one scenario file."""
    raw = json.loads(path.read_text())
    _validate(raw, path)
    steps = tuple(_step(entry) for entry in raw["steps"])
    _refuse_duplicates(steps, path)
    expectations = tuple(parse_expectation(entry) for entry in raw["expect"])
    _refuse_unknown_steps(steps, expectations, path)
    _refuse_unresolvable_references(steps, path)
    return Scenario(
        id=raw["id"],
        title=raw["title"],
        timeout_ms=int(raw.get("timeout_ms", 15000)),
        requires=tuple(raw.get("requires", ())),
        auth=raw.get("auth", "dev_token"),
        mutates=bool(raw.get("mutates", False)),
        steps=steps,
        expectations=expectations,
        raw=raw,
        path=path,
    )


def load_scenarios(directory: Path = SCENARIO_DIR) -> list[Scenario]:
    """Every scenario in *directory*, in the order their names give them."""
    return [load_scenario(path) for path in sorted(directory.glob("*.json"))]


def _validate(raw: Mapping[str, Any], path: Path) -> None:
    """Hold the file to the committed schema and to its own file name."""
    try:
        jsonschema.validate(raw, schema("scenario"))
    except jsonschema.ValidationError as error:
        raise ValueError(f"{path.name} does not match the scenario schema: {error.message}") from error
    if raw["id"] != path.stem:
        raise ValueError(f"{path.name}: id {raw['id']!r} does not match the file name")


def _step(entry: Mapping[str, Any]) -> Step:
    """One step, with the arguments its action cannot do without."""
    action = entry["action"]
    step = Step(
        id=entry["id"],
        action=action,
        auth=entry.get("auth", "token"),
        path=entry.get("path"),
        session_id=entry.get("session_id"),
        body=entry.get("body"),
        volatile=tuple(entry.get("volatile", ())),
        worker_id=entry.get("worker_id"),
        hijack_id=entry.get("hijack_id"),
        owner=entry.get("owner"),
        lease_s=entry.get("lease_s"),
        keys=entry.get("keys"),
        input_mode=entry.get("input_mode"),
        limit=entry.get("limit"),
        repeat=int(entry.get("repeat", 1)),
    )
    if action in _NEEDS_PATH and step.path is None:
        raise ValueError(f"step {step.id!r}: {action} needs a path")
    if action in _NEEDS_SESSION and step.session_id is None:
        raise ValueError(f"step {step.id!r}: {action} needs a session_id")
    if action in _NEEDS_WORKER and step.worker_id is None:
        raise ValueError(f"step {step.id!r}: {action} needs a worker_id")
    if action in _NEEDS_LEASE and step.hijack_id is None:
        raise ValueError(f"step {step.id!r}: {action} needs a hijack_id")
    if action == "hijack_send" and step.keys is None:
        raise ValueError(f"step {step.id!r}: hijack_send needs keys to send")
    return step


def _refuse_unresolvable_references(steps: Sequence[Step], path: Path) -> None:
    """A ``${step.path}`` reference must name a step that has already run.

    Four drivers each resolve these for themselves, so each would discover a
    bad one at run time and word the failure differently. It is a malformed
    scenario rather than anything a server did, so it is refused once, here.
    """
    ran: set[str] = set()
    repeated: set[str] = set()
    for step in steps:
        for field in (step.hijack_id, step.session_id, step.worker_id, step.keys, step.owner):
            match = _REFERENCE.match(field) if isinstance(field, str) else None
            if match is None:
                continue
            if match.group(1) not in ran:
                raise ValueError(
                    f"{path.name}: step {step.id!r} refers to {match.group(1)!r}, which has not run by then"
                )
            if match.group(1) in repeated:
                # A repeated step has as many answers as repetitions, and the
                # grammar cannot say which is meant: the step id part admits no
                # dot, so `${flood.2.body.x}` reads as step `flood`, path
                # `2.body.x`. Rather than let four drivers each pick a reading,
                # refuse it — and if a scenario ever needs the value, the step
                # that produces it should not be the repeated one.
                raise ValueError(
                    f"{path.name}: step {step.id!r} refers to {match.group(1)!r}, which is repeated "
                    f"and so has no single answer to refer to"
                )
        ran.add(step.id)
        if step.repeat > 1:
            repeated.add(step.id)


def _refuse_duplicates(steps: Sequence[Step], path: Path) -> None:
    """Observations are keyed by step id, so two of one id lose one of them."""
    seen: set[str] = set()
    for step in steps:
        if step.id in seen:
            raise ValueError(f"{path.name}: step {step.id!r} appears twice")
        seen.add(step.id)


def _refuse_unknown_steps(steps: Sequence[Step], expectations: Sequence[Expectation], path: Path) -> None:
    """An expectation about a step nobody runs would pass in every cell."""
    known = {observed for step in steps for observed in step.observation_ids}
    repeated = {step.id for step in steps if step.repeat > 1}
    for expectation in expectations:
        if expectation.step.endswith(ANY_REPETITION):
            # `<id>.*` means "some repetition", which only says anything about a
            # step that has repetitions.
            base = expectation.step[: -len(ANY_REPETITION)]
            if base not in repeated:
                raise ValueError(
                    f"{path.name}: expectation names {expectation.step!r}, but {base!r} is not a repeated step — "
                    f"a wildcard over one observation is just that observation"
                )
            continue
        if expectation.step in repeated:
            # The bare id of a repeated step records nothing. Left unrefused
            # this reads as an expectation nobody runs, which is the one
            # failure that passes everywhere at once.
            raise ValueError(
                f"{path.name}: expectation names step {expectation.step!r}, which is repeated — "
                f"name the repetition it means, such as {expectation.step}.0"
            )
        if expectation.step not in known:
            raise ValueError(
                f"{path.name}: expectation names step {expectation.step!r}, which the scenario does not run"
            )
