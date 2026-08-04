#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Every client against every server.

A cell is one client language talking to one server language over a real
socket, running one scenario. A cell passes when two separate things hold:

1. every expectation the scenario wrote down held, and
2. what it observed matches what the reference pair observed.

The second is what earns the matrix its keep. A scenario can only assert what
somebody thought to assert; agreement catches the fields nobody thought about,
which is where parity actually drifts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Protocol

from harness import drivers
from harness.drivers import DriverError, DriverSpec
from harness.expectations import Failure, check_all
from harness.normalize import Difference, differences, observations

if TYPE_CHECKING:
    from harness.scenario import Scenario

#: The language every other cell is compared against, when it is in the run.
REFERENCE_LANGUAGE: Final = "python"

PASS: Final = "pass"  # noqa: S105 - a verdict, not a credential
FAIL: Final = "fail"
ERROR: Final = "error"
UNSUPPORTED: Final = "unsupported"

#: A cell, named by which server and which client met in it.
Pair = tuple[str, str]


class Runner(Protocol):
    """The process mechanics, so the orchestration can be tested without them."""

    def start_server(self, spec: DriverSpec, **kwargs: Any) -> Any: ...

    def run_client(self, spec: DriverSpec, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Cell:
    """One client language against one server language, on one scenario."""

    scenario_id: str
    server: str
    client: str
    status: str
    failures: tuple[Failure, ...] = ()
    differences: tuple[Difference, ...] = ()
    detail: str | None = None

    @property
    def counts_against_the_run(self) -> bool:
        return self.status in {FAIL, ERROR}


@dataclass(frozen=True)
class MatrixReport:
    """Every cell that ran."""

    cells: tuple[Cell, ...]

    @property
    def ok(self) -> bool:
        return not any(cell.counts_against_the_run for cell in self.cells)

    def by_scenario(self, scenario_id: str) -> tuple[Cell, ...]:
        return tuple(cell for cell in self.cells if cell.scenario_id == scenario_id)


class _Drivers:
    """The real mechanics: subprocesses, sockets, deadlines."""

    def start_server(self, spec: DriverSpec, **kwargs: Any) -> Any:
        return drivers.start_server(spec, **kwargs)

    def run_client(self, spec: DriverSpec, **kwargs: Any) -> dict[str, Any]:
        kwargs.pop("server_language", None)
        return drivers.run_client(spec, **kwargs)


def run_matrix(
    scenarios: Iterable[Scenario],
    *,
    servers: Sequence[DriverSpec],
    clients: Sequence[DriverSpec],
    runner: Runner | None = None,
) -> MatrixReport:
    """Run every scenario in every cell and say what happened."""
    mechanics: Runner = runner if runner is not None else _Drivers()
    cells: list[Cell] = []
    for scenario in scenarios:
        cells.extend(_scenario(scenario, servers, clients, mechanics))
    return MatrixReport(tuple(cells))


def _scenario(
    scenario: Scenario, servers: Sequence[DriverSpec], clients: Sequence[DriverSpec], mechanics: Runner
) -> list[Cell]:
    """One scenario in every cell, then every cell held to the reference."""
    cells: list[Cell] = []
    seen: dict[Pair, dict[str, Any]] = {}
    for server in servers:
        cells.extend(_row(scenario, server, clients, mechanics, seen))
    return _with_agreement(cells, seen)


def _row(
    scenario: Scenario,
    server: DriverSpec,
    clients: Sequence[DriverSpec],
    mechanics: Runner,
    seen: dict[Pair, dict[str, Any]],
) -> list[Cell]:
    """One server, every client.

    Standing a server up is the expensive part of a cell, so a row normally
    pays for it once and shares it. A scenario that declares it *mutates* gets
    a server per client instead: one that puts a session into hijack mode and
    takes its lease would otherwise leave the next client starting from
    somewhere the scenario never described — its first step answered by a
    server the previous client had already changed.
    """
    if scenario.mutates:
        return [_isolated_cell(scenario, server, client, mechanics, seen) for client in clients]
    timeout_s = scenario.timeout_ms / 1000
    try:
        with mechanics.start_server(server, auth=scenario.auth, timeout_s=timeout_s) as running:
            return [_run_cell(scenario, server, client, mechanics, running, seen) for client in clients]
    except DriverError as error:
        return [Cell(scenario.id, server.language, client.language, ERROR, detail=str(error)) for client in clients]


def _isolated_cell(
    scenario: Scenario,
    server: DriverSpec,
    client: DriverSpec,
    mechanics: Runner,
    seen: dict[Pair, dict[str, Any]],
) -> Cell:
    """One client against a server of its own, for a scenario that changes it."""
    try:
        with mechanics.start_server(server, auth=scenario.auth, timeout_s=scenario.timeout_ms / 1000) as running:
            return _run_cell(scenario, server, client, mechanics, running, seen)
    except DriverError as error:
        return Cell(scenario.id, server.language, client.language, ERROR, detail=str(error))


def _run_cell(
    scenario: Scenario,
    server: DriverSpec,
    client: DriverSpec,
    mechanics: Runner,
    running: Any,
    seen: dict[Pair, dict[str, Any]],
) -> Cell:
    """One client against one running server, judged against the scenario."""
    missing = _missing_capabilities(scenario.requires, running.capabilities)
    if missing:
        return Cell(
            scenario.id,
            server.language,
            client.language,
            UNSUPPORTED,
            detail=f"{server.language} server does not serve required capabilities: {', '.join(missing)}",
        )
    missing = _missing_capabilities(scenario.requires, client.client_capabilities)
    if missing:
        return Cell(
            scenario.id,
            server.language,
            client.language,
            UNSUPPORTED,
            detail=f"{client.language} client does not support required capabilities: {', '.join(missing)}",
        )
    try:
        result = mechanics.run_client(
            client,
            scenario_path=scenario.path,
            base_url=running.base_url,
            token=running.token,
            timeout_s=scenario.timeout_ms / 1000,
            server_language=server.language,
        )
    except DriverError as error:
        return Cell(scenario.id, server.language, client.language, ERROR, detail=str(error))
    return _judge(scenario, server.language, client.language, result, seen)


def _judge(
    scenario: Scenario,
    server: str,
    client: str,
    result: dict[str, Any],
    seen: dict[Pair, dict[str, Any]],
) -> Cell:
    """A cell's own verdict, before it is compared with anything."""
    missing = _missing_capabilities(scenario.requires, result.get("capabilities", ()))
    if missing:
        return Cell(
            scenario.id,
            server,
            client,
            UNSUPPORTED,
            detail=f"{client} client does not support required capabilities: {', '.join(missing)}",
        )
    reported = result.get("status")
    if reported in {UNSUPPORTED, ERROR}:
        return Cell(scenario.id, server, client, str(reported), detail=result.get("error"))
    # Two different readings of the same result, deliberately. An
    # expectation is this cell's own contract and must see what the server
    # actually said — a scenario has to be able to assert that a lease expiry
    # is a *number* even though no two runs agree on which number. Masking
    # exists only so two cells can be compared, so it applies only there.
    watched = observations(result, {})
    seen[(server, client)] = observations(result, scenario.volatile_by_step)
    failures = check_all(scenario.expectations, watched)
    return Cell(scenario.id, server, client, FAIL if failures else PASS, failures=failures)


def _missing_capabilities(required: Sequence[str], offered: Iterable[str]) -> tuple[str, ...]:
    """Required capabilities absent from one side of a live cell."""
    available = frozenset(offered)
    return tuple(capability for capability in required if capability not in available)


def _with_agreement(cells: Sequence[Cell], seen: dict[Pair, dict[str, Any]]) -> list[Cell]:
    """Fold cross-cell agreement in, once every cell has something to say.

    Agreement is a property of the matrix rather than of any one cell: a cell
    has to be measured against something, and what it is measured against only
    exists after the run.
    """
    reference = _reference(cells, seen)
    if reference is None:
        return list(cells)
    return [_compared(cell, seen, reference) for cell in cells]


def _reference(cells: Sequence[Cell], seen: dict[Pair, dict[str, Any]]) -> Pair | None:
    """The cell every other is held to: the reference pair, or the first."""
    preferred = (REFERENCE_LANGUAGE, REFERENCE_LANGUAGE)
    if preferred in seen:
        return preferred
    for cell in cells:
        if (cell.server, cell.client) in seen:
            return (cell.server, cell.client)
    return None


def _compared(cell: Cell, seen: dict[Pair, dict[str, Any]], reference: Pair) -> Cell:
    """A cell, plus wherever it disagreed with the reference cell."""
    key = (cell.server, cell.client)
    if key == reference or key not in seen:
        return cell
    found = tuple(differences(seen[reference], seen[key]))
    if not found:
        return cell
    return replace(cell, status=FAIL, differences=found)
