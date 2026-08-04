#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""What a scenario's expectations mean.

Drivers observe; the harness judges. This module is the whole of the judging,
so that four languages cannot disagree about what an expectation *means* —
only about what their server actually did.

Two readings matter more than they look:

* ``True`` is not ``1``. Python says it is, JSON says it is not, and a server
  answering ``true`` where the contract says ``1`` has diverged.
* A field that is absent is not a field whose value is ``null``. A scenario
  has to be able to tell those apart, so a missing value is :data:`MISSING`
  rather than ``None``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final


class _Missing:
    """The absence of a value, which is not the same as ``None``."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "<missing>"


MISSING: Final = _Missing()

#: Every predicate a scenario may use, in the order they are looked for.
PREDICATES: Final = ("equals", "in", "type", "matches", "min_count", "present")


@dataclass(frozen=True)
class Expectation:
    """One assertion about one step's observations."""

    step: str
    path: str
    predicate: str
    expected: Any
    why: str | None


@dataclass(frozen=True)
class Failure:
    """An expectation that did not hold, with enough to act on."""

    expectation: Expectation
    actual: Any
    message: str

    @property
    def expected(self) -> Any:
        return self.expectation.expected


def parse_expectation(raw: Mapping[str, Any]) -> Expectation:
    """Read one ``expect`` entry, refusing the shapes that would be ambiguous."""
    given = [name for name in PREDICATES if name in raw]
    if not given:
        raise ValueError(f"expectation on step {raw.get('step')!r} names no predicate")
    if len(given) > 1:
        raise ValueError(
            f"expectation on step {raw.get('step')!r} names {len(given)} predicates, expected one predicate"
        )
    name = given[0]
    return Expectation(
        step=str(raw["step"]),
        path=str(raw["path"]),
        predicate=name,
        expected=raw[name],
        why=raw.get("why"),
    )


def resolve(fields: Any, path: str) -> Any:
    """Read *path* out of *fields*, or :data:`MISSING` if it is not there.

    A numeric segment indexes a list, but on a mapping it is a key: a server
    is free to answer with an object keyed by digits, and reading that as an
    index would look at something else entirely.
    """
    if path == "":
        return fields
    value: Any = fields
    for segment in path.split("."):
        if isinstance(value, Mapping):
            if segment not in value:
                return MISSING
            value = value[segment]
        elif isinstance(value, (list, tuple)) and segment.isdigit():
            index = int(segment)
            if index >= len(value):
                return MISSING
            value = value[index]
        else:
            return MISSING
    return value


def check(expectation: Expectation, steps: Mapping[str, Mapping[str, Any]]) -> Failure | None:
    """Evaluate *expectation* against the steps a driver recorded."""
    if expectation.step not in steps:
        return Failure(
            expectation,
            MISSING,
            f"no step named {expectation.step!r} ran; the scenario names a step nobody performed",
        )
    actual = resolve(steps[expectation.step], expectation.path)
    held, detail = _HOLDS[expectation.predicate](actual, expectation.expected)
    if held:
        return None
    return Failure(expectation, actual, f"{expectation.step}.{expectation.path}: {detail}")


#: The suffix that turns a repeated step's name into "some repetition of it".
ANY_REPETITION: Final = ".*"


def check_all(expectations: Sequence[Expectation], steps: Mapping[str, Mapping[str, Any]]) -> tuple[Failure, ...]:
    """Evaluate every expectation, resolving ``<step>.*`` against the repetitions.

    A named step is checked on its own, as it always was. A wildcard step is a
    claim about *some* repetition, and every wildcard expectation naming the
    same step has to hold of one and the same repetition — otherwise "a refusal
    carries its own reason" would be satisfied by a 429 in one repetition and
    the word ``rate_limited`` in another, which is a different and much weaker
    claim.

    A repeated step exists where the answers stop being the same, and some of
    those sequences are timed rather than counted: which repetition of a flood
    first finds the budget gone depends on how long the flood took. Pinning an
    index into one asserts how fast the runner was.
    """
    failures: list[Failure] = []
    grouped: dict[str, list[Expectation]] = {}

    for expectation in expectations:
        if expectation.step.endswith(ANY_REPETITION):
            grouped.setdefault(expectation.step, []).append(expectation)
        else:
            found = check(expectation, steps)
            if found is not None:
                failures.append(found)

    for step, group in grouped.items():
        found = _check_any_repetition(step, group, steps)
        if found is not None:
            failures.append(found)

    return tuple(failures)


def _check_any_repetition(
    step: str, group: Sequence[Expectation], steps: Mapping[str, Mapping[str, Any]]
) -> Failure | None:
    """Hold *group* against each repetition of *step*, needing one to satisfy all."""
    base = step[: -len(ANY_REPETITION)]
    prefix = f"{base}."
    # `flood.` rather than `flood`, so `flood.*` never picks up `floodgate.0`.
    candidates = sorted(name for name in steps if name.startswith(prefix))

    for name in candidates:
        if all(check(_renamed(one, name), steps) is None for one in group):
            return None

    described = ", ".join(f"{one.path} {one.predicate} {one.expected!r}" for one in group)
    seen = len(candidates)
    return Failure(
        group[0],
        MISSING,
        f"no repetition of {base!r} satisfied all of [{described}] "
        f"({seen} repetition{'' if seen == 1 else 's'} recorded)",
    )


def _renamed(expectation: Expectation, step: str) -> Expectation:
    """*expectation* as though it had named *step* all along."""
    return Expectation(
        step=step,
        path=expectation.path,
        predicate=expectation.predicate,
        expected=expectation.expected,
        why=expectation.why,
    )


def _json_equal(left: Any, right: Any) -> bool:
    """Equality as JSON reads it: a boolean is never a number."""
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return left.keys() == right.keys() and all(_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, _Missing) or isinstance(right, _Missing):
        return left is right
    return bool(left == right)


def _json_type(value: Any) -> str:
    """The JSON type name of *value*, or ``missing``."""
    if isinstance(value, _Missing):
        return "missing"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    return "object"


def _holds_equals(actual: Any, expected: Any) -> tuple[bool, str]:
    if _json_equal(actual, expected):
        return True, ""
    return False, f"expected {expected!r}, saw {actual!r}"


def _holds_in(actual: Any, expected: Any) -> tuple[bool, str]:
    if any(_json_equal(actual, candidate) for candidate in expected):
        return True, ""
    return False, f"expected one of {expected!r}, saw {actual!r}"


def _holds_type(actual: Any, expected: Any) -> tuple[bool, str]:
    seen = _json_type(actual)
    if seen == expected:
        return True, ""
    return False, f"expected a {expected}, saw a {seen} ({actual!r})"


def _holds_matches(actual: Any, expected: Any) -> tuple[bool, str]:
    if not isinstance(actual, str):
        return False, f"expected text matching {expected!r}, saw a {_json_type(actual)} ({actual!r})"
    if re.search(expected, actual) is not None:
        return True, ""
    return False, f"expected text matching {expected!r}, saw {actual!r}"


def _holds_min_count(actual: Any, expected: Any) -> tuple[bool, str]:
    if isinstance(actual, (list, tuple, str, Mapping)):
        if len(actual) >= expected:
            return True, ""
        return False, f"expected at least {expected}, counted {len(actual)}"
    return False, f"expected something countable, saw a {_json_type(actual)} ({actual!r})"


def _holds_present(actual: Any, expected: Any) -> tuple[bool, str]:
    there = not isinstance(actual, _Missing)
    if there is bool(expected):
        return True, ""
    return False, "expected the field to be there, it was absent" if expected else f"expected no field, saw {actual!r}"


_HOLDS: Final[dict[str, Any]] = {
    "equals": _holds_equals,
    "in": _holds_in,
    "type": _holds_type,
    "matches": _holds_matches,
    "min_count": _holds_min_count,
    "present": _holds_present,
}
