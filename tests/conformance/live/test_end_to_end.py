#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The harness against a real server, and against a server that lies.

Everything else in this directory tests the harness's parts. This runs the
whole of it — a real reference server on a real ephemeral port, a real client
in another process — and then does the thing a parity harness is worthless
without: proves it *fails* when a cell diverges.

A harness nobody has watched fail is a harness nobody knows works.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from harness.drivers import DriverSpec
from harness.matrix import run_matrix
from harness.registry import REPO_ROOT, available
from harness.scenario import SCENARIO_DIR, load_scenarios

pytestmark = pytest.mark.slow

_REAL_DRIVER = REPO_ROOT / "conformance/live/drivers/python/driver.py"


def _python() -> DriverSpec:
    return DriverSpec(language="python", command=(sys.executable, str(_REAL_DRIVER)), cwd=REPO_ROOT)


def _liar(tmp_path: Path, patch: str) -> DriverSpec:
    """A client driver that runs the real one, then alters what it reports.

    It is the only way to prove the comparison bites: a server that diverges
    is exactly a cell whose observations differ from the reference's, and this
    manufactures one without having to break a real server to do it.
    """
    script = tmp_path / "liar.py"
    script.write_text(
        "import json, subprocess, sys\n"
        f"real = {str(_REAL_DRIVER)!r}\n"
        "done = subprocess.run([sys.executable, real, *sys.argv[1:]], capture_output=True, text=True)\n"
        "lines = [line for line in done.stdout.splitlines() if line.strip()]\n"
        "result = json.loads(lines[-1])\n"
        f"{patch}\n"
        "print(json.dumps(result))\n"
    )
    return DriverSpec(language="go", command=(sys.executable, str(script)), cwd=REPO_ROOT)


@pytest.fixture(scope="module")
def health_scenario() -> object:
    return next(one for one in load_scenarios(SCENARIO_DIR) if one.id == "001_health")


class TestAgainstTheRealServer:
    def test_the_reference_talks_to_itself(self, health_scenario: object) -> None:
        report = run_matrix([health_scenario], servers=[_python()], clients=[_python()])
        (cell,) = report.cells
        assert cell.status == "pass", cell.failures
        assert report.ok

    def test_every_committed_scenario_holds(self) -> None:
        # The scenarios are the contract. If the reference itself does not
        # satisfy them, they are asserting something nobody implements.
        report = run_matrix(load_scenarios(SCENARIO_DIR), servers=[_python()], clients=[_python()])
        assert report.ok, [
            (cell.scenario_id, [failure.message for failure in cell.failures])
            for cell in report.cells
            if cell.status != "pass"
        ]

    def test_the_registry_can_start_what_it_advertises(self, health_scenario: object) -> None:
        # A registry entry that points at nothing would report a language as
        # available and then error every cell in its row.
        found = available(REPO_ROOT, only=["python"])
        report = run_matrix([health_scenario], servers=found.servers, clients=found.clients)
        assert report.ok


class TestItCatchesADivergence:
    def test_a_client_reporting_a_different_body_fails_even_though_the_server_is_fine(
        self, health_scenario: object, tmp_path: Path
    ) -> None:
        # Both cells satisfy every expectation the scenario wrote down — the
        # altered field is one nobody asserted on. Only the comparison against
        # the reference cell catches it, which is the whole argument for
        # having one.
        liar = _liar(tmp_path, "result['steps'][0]['fields']['body']['service'] = 'something-else'")
        report = run_matrix([health_scenario], servers=[_python()], clients=[_python(), liar])
        divergent = next(cell for cell in report.cells if cell.client == "go")
        assert divergent.status == "fail"
        assert any("service" in difference.path for difference in divergent.differences)
        assert not report.ok

    def test_a_field_one_side_omits_is_caught(self, health_scenario: object, tmp_path: Path) -> None:
        # A dropped field is the quietest divergence there is: every
        # expectation about the fields that remain still holds.
        liar = _liar(tmp_path, "result['steps'][0]['fields']['body'].pop('control_plane_backend')")
        report = run_matrix([health_scenario], servers=[_python()], clients=[_python(), liar])
        divergent = next(cell for cell in report.cells if cell.client == "go")
        assert divergent.status == "fail"
        assert not report.ok

    def test_a_volatile_field_may_differ_without_failing(self, health_scenario: object, tmp_path: Path) -> None:
        # uptime_s is declared volatile by the scenario, so a cell that saw a
        # different clock is not a cell that saw a different server.
        liar = _liar(tmp_path, "result['steps'][0]['fields']['body']['uptime_s'] = 999999.5")
        report = run_matrix([health_scenario], servers=[_python()], clients=[_python(), liar])
        assert report.ok

    def test_a_status_code_that_differs_is_caught(self, tmp_path: Path) -> None:
        refusals = next(one for one in load_scenarios(SCENARIO_DIR) if one.id == "002_session_authz")
        liar = _liar(tmp_path, "result['steps'][0]['fields']['status'] = 403")
        report = run_matrix([refusals], servers=[_python()], clients=[_python(), liar])
        divergent = next(cell for cell in report.cells if cell.client == "go")
        # This one the scenario *does* assert on, so it fails twice over —
        # once against the contract and once against the reference cell.
        assert divergent.status == "fail"
        assert divergent.failures or divergent.differences
