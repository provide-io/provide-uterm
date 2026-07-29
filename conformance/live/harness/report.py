#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Printing a matrix run.

A cell that fails is read by somebody who did not write the scenario, so a
failure prints three things: what was expected, what was seen, and the
scenario's own reason the contract says so.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
from typing import Final

from harness.matrix import ERROR, FAIL, PASS, UNSUPPORTED, Cell, MatrixReport

_MARK: Final = {PASS: "ok", FAIL: "FAIL", ERROR: "ERR", UNSUPPORTED: "n/a"}


def render(report: MatrixReport, *, gaps: Sequence[str] = ()) -> str:
    """The whole run as text: a grid per scenario, then every failure."""
    lines: list[str] = []
    for scenario_id in _scenario_ids(report.cells):
        lines.extend(_grid(scenario_id, report.by_scenario(scenario_id)))
        lines.append("")
    lines.extend(_details(report.cells))
    lines.extend(_gaps(gaps))
    lines.append(_summary(report, gaps))
    return "\n".join(lines)


def _scenario_ids(cells: Iterable[Cell]) -> list[str]:
    """Every scenario that ran, in the order it first appears."""
    seen: dict[str, None] = {}
    for cell in cells:
        seen.setdefault(cell.scenario_id, None)
    return list(seen)


def _grid(scenario_id: str, cells: Sequence[Cell]) -> list[str]:
    """One scenario as a server-by-client grid."""
    servers = _ordered(cell.server for cell in cells)
    clients = _ordered(cell.client for cell in cells)
    width = max((len(name) for name in servers), default=0) + 2
    header = " " * width + "".join(f"{name:>12}" for name in clients)
    lines = [f"{scenario_id}", f"{'':{width}}{'client →':>12}" if clients else "", header]
    for server in servers:
        row = f"{server:<{width}}"
        for client in clients:
            found = next((cell for cell in cells if cell.server == server and cell.client == client), None)
            row += f"{_MARK.get(found.status, '?') if found else '-':>12}"
        lines.append(row)
    return [line for line in lines if line]


def _ordered(names: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for name in names:
        seen.setdefault(name, None)
    return list(seen)


def _details(cells: Iterable[Cell]) -> list[str]:
    """Everything that went wrong, at enough length to act on."""
    lines: list[str] = []
    for cell in cells:
        if cell.status == PASS:
            continue
        lines.append(
            f"[{_MARK.get(cell.status, cell.status)}] {cell.scenario_id}: {cell.client} client → {cell.server} server"
        )
        if cell.detail:
            lines.append(f"    {cell.detail}")
        lines.extend(_failures(cell))
        lines.extend(_differences(cell))
        lines.append("")
    return lines


def _failures(cell: Cell) -> list[str]:
    lines: list[str] = []
    for failure in cell.failures:
        lines.append(f"    expectation: {failure.message}")
        if failure.expectation.why:
            lines.append(f"        why: {failure.expectation.why}")
    return lines


def _differences(cell: Cell) -> list[str]:
    """Where a cell disagreed with the reference, whatever its own checks said."""
    if not cell.differences:
        return []
    lines = ["    disagrees with the reference cell:"]
    for difference in cell.differences:
        lines.append(f"        {difference.path}: reference {difference.left!r}, this cell {difference.right!r}")
    return lines


def _gaps(gaps: Sequence[str]) -> list[str]:
    """What did not run. Printed always: a silent gap is how a matrix lies."""
    if not gaps:
        return []
    return ["not run:", *(f"    {gap}" for gap in gaps), ""]


def _summary(report: MatrixReport, gaps: Sequence[str]) -> str:
    counted = {status: sum(1 for cell in report.cells if cell.status == status) for status in _MARK}
    return (
        f"{len(report.cells)} cells: {counted[PASS]} ok, {counted[FAIL]} failed, "
        f"{counted[ERROR]} errored, {counted[UNSUPPORTED]} unsupported; {len(gaps)} not run"
    )
