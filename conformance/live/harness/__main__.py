#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Run the live matrix.

    python conformance/live/harness --list-drivers
    python conformance/live/harness
    python conformance/live/harness --scenario 002_session_authz --servers python

Exits non-zero when any cell failed or errored. A cell that reported an
unsupported capability does not fail the run, but it is always printed: a
silently skipped cell is how a matrix comes to mean nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

from harness.matrix import run_matrix
from harness.registry import LANGUAGES, REPO_ROOT, available
from harness.report import render
from harness.scenario import SCENARIO_DIR, load_scenarios


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="live-matrix", description="every client against every server")
    parser.add_argument("--scenario", action="append", help="run only this scenario id (repeatable)")
    parser.add_argument("--servers", nargs="*", choices=LANGUAGES, help="limit the server languages")
    parser.add_argument("--clients", nargs="*", choices=LANGUAGES, help="limit the client languages")
    parser.add_argument("--list-drivers", action="store_true", help="say what can run, and what cannot")
    parser.add_argument("--json", action="store_true", help="write the cells as JSON instead of a grid")
    args = parser.parse_args(argv)

    servers = available(REPO_ROOT, only=args.servers)
    clients = available(REPO_ROOT, only=args.clients)
    gaps = tuple(dict.fromkeys((*servers.gaps, *clients.gaps)))

    if args.list_drivers:
        return _list(servers, clients, gaps)

    scenarios = [
        scenario
        for scenario in load_scenarios(SCENARIO_DIR)
        if args.scenario is None or scenario.id in set(args.scenario)
    ]
    if not scenarios:
        print("no scenarios matched", file=sys.stderr)
        return 2
    if not servers.servers or not clients.clients:
        print("nothing to run:\n  " + "\n  ".join(gaps), file=sys.stderr)
        return 2

    report = run_matrix(scenarios, servers=servers.servers, clients=clients.clients)
    if args.json:
        print(json.dumps([_as_json(cell) for cell in report.cells], indent=2))
    else:
        print(render(report, gaps=gaps))
    return 0 if report.ok else 1


def _list(servers: object, clients: object, gaps: Sequence[str]) -> int:
    print("servers:", ", ".join(spec.language for spec in servers.servers) or "none")  # type: ignore[attr-defined]
    print("clients:", ", ".join(spec.language for spec in clients.clients) or "none")  # type: ignore[attr-defined]
    for gap in gaps:
        print("  not available:", gap)
    return 0


def _as_json(cell: object) -> dict[str, object]:
    return {
        "scenario_id": cell.scenario_id,  # type: ignore[attr-defined]
        "server": cell.server,  # type: ignore[attr-defined]
        "client": cell.client,  # type: ignore[attr-defined]
        "status": cell.status,  # type: ignore[attr-defined]
        "detail": cell.detail,  # type: ignore[attr-defined]
        "failures": [failure.message for failure in cell.failures],  # type: ignore[attr-defined]
        "differences": [  # type: ignore[attr-defined]
            {"path": one.path, "reference": one.left, "cell": one.right} for one in cell.differences
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
