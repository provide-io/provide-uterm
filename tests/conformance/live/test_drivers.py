#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for starting drivers and reading what they say.

These run real processes — the whole point of the live harness is that no
part of it is in-process — but the processes are tiny stand-ins written by the
test, so what is under test here is the harness's half of the protocol and not
any language's server.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from harness.drivers import DriverError, DriverSpec, run_client, start_server

_RESULT = {
    "scenario_id": "001_example",
    "language": "python",
    "role": "client",
    "status": "completed",
    "steps": [{"id": "health", "fields": {"status": 200, "ok": True, "body": {"status": "ok"}}}],
}


def _script(tmp_path: Path, body: str) -> DriverSpec:
    """A driver that is whatever the test says it is."""
    path = tmp_path / "driver.py"
    path.write_text(body)
    return DriverSpec(language="python", command=(sys.executable, str(path)), cwd=None)


class TestStartServer:
    def test_reads_the_announcement(self, tmp_path: Path) -> None:
        spec = _script(
            tmp_path,
            "import json,sys\n"
            'print(json.dumps({"role":"server","language":"python","base_url":"http://127.0.0.1:1",'
            '"token":"t","capabilities":["hijack.rest"]}), flush=True)\n'
            "sys.stdin.read()\n",
        )
        with start_server(spec, auth="dev_token", timeout_s=10) as server:
            assert server.base_url == "http://127.0.0.1:1"
            assert server.token == "t"
            assert server.capabilities == ("hijack.rest",)

    def test_ignores_noise_before_the_announcement(self, tmp_path: Path) -> None:
        # A runtime that logs on startup is ordinary; the announcement is the
        # first line that parses as one.
        spec = _script(
            tmp_path,
            "import json,sys\n"
            'print("listening...", flush=True)\n'
            'print(json.dumps({"role":"server","language":"python","base_url":"http://127.0.0.1:2","token":"t"}), flush=True)\n'
            "sys.stdin.read()\n",
        )
        with start_server(spec, auth="dev_token", timeout_s=10) as server:
            assert server.base_url == "http://127.0.0.1:2"

    def test_a_driver_that_says_nothing_is_an_error_not_a_hang(self, tmp_path: Path) -> None:
        spec = _script(tmp_path, "import sys\nsys.stdin.read()\n")
        with pytest.raises(DriverError, match="did not announce"):
            with start_server(spec, auth="dev_token", timeout_s=1):
                pass  # pragma: no cover - the context manager raises on entry

    def test_a_driver_that_dies_reports_what_it_said(self, tmp_path: Path) -> None:
        spec = _script(tmp_path, "import sys\nsys.stderr.write('no port for you\\n')\nraise SystemExit(3)\n")
        with pytest.raises(DriverError, match="no port for you"):
            with start_server(spec, auth="dev_token", timeout_s=10):
                pass  # pragma: no cover - the context manager raises on entry

    def test_an_announcement_that_is_not_a_server_line_is_refused(self, tmp_path: Path) -> None:
        spec = _script(tmp_path, "import json,sys\nprint(json.dumps({'nope': 1}), flush=True)\nsys.stdin.read()\n")
        with pytest.raises(DriverError, match="did not announce"):
            with start_server(spec, auth="dev_token", timeout_s=1):
                pass  # pragma: no cover - the context manager raises on entry

    def test_leaving_the_block_stops_the_server(self, tmp_path: Path) -> None:
        spec = _script(
            tmp_path,
            "import json,sys\n"
            'print(json.dumps({"role":"server","language":"python","base_url":"http://127.0.0.1:3","token":"t"}), flush=True)\n'
            "sys.stdin.read()\n",
        )
        with start_server(spec, auth="dev_token", timeout_s=10) as server:
            handle = server
        assert handle.returncode is not None

    def test_a_server_that_ignores_the_close_is_killed(self, tmp_path: Path) -> None:
        # Closing stdin is the polite ask. A driver that does not take it must
        # not be able to hold a CI job open.
        spec = _script(
            tmp_path,
            "import json,signal,sys,time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            'print(json.dumps({"role":"server","language":"python","base_url":"http://127.0.0.1:4","token":"t"}), flush=True)\n'
            "time.sleep(60)\n",
        )
        with start_server(spec, auth="dev_token", timeout_s=10, grace_s=0.5) as server:
            handle = server
        assert handle.returncode is not None

    def test_the_auth_mode_reaches_the_driver(self, tmp_path: Path) -> None:
        spec = _script(
            tmp_path,
            "import json,sys\n"
            'print(json.dumps({"role":"server","language":"python","base_url":"http://x","token":" ".join(sys.argv[1:])}), flush=True)\n'
            "sys.stdin.read()\n",
        )
        with start_server(spec, auth="jwt", timeout_s=10) as server:
            assert server.token == "serve --auth jwt"


class TestRunClient:
    def test_returns_the_result(self, tmp_path: Path) -> None:
        spec = _script(tmp_path, f"import json\nprint(json.dumps({_RESULT!r}))\n")
        assert (
            run_client(spec, scenario_path=tmp_path / "s.json", base_url="http://x", token="t", timeout_s=10) == _RESULT
        )

    def test_passes_the_scenario_and_connection_on_the_command_line(self, tmp_path: Path) -> None:
        spec = _script(tmp_path, "import json,sys\nprint(json.dumps({'argv': sys.argv[1:]}))\n")
        with pytest.raises(DriverError) as caught:
            run_client(spec, scenario_path=tmp_path / "s.json", base_url="http://x", token="t", timeout_s=10)
        # The result is refused (it is not a result), but the argv it echoed
        # is in the refusal, which is what this is checking.
        assert "--base-url" in str(caught.value)
        assert "--token" in str(caught.value)

    def test_a_result_that_is_not_json_is_refused(self, tmp_path: Path) -> None:
        spec = _script(tmp_path, "print('not json')\n")
        with pytest.raises(DriverError, match="not JSON"):
            run_client(spec, scenario_path=tmp_path / "s.json", base_url="http://x", token="t", timeout_s=10)

    def test_a_result_the_schema_rejects_is_refused(self, tmp_path: Path) -> None:
        # A driver inventing its own shape would compare against nothing.
        broken = {**_RESULT, "status": "pass"}
        spec = _script(tmp_path, f"import json\nprint(json.dumps({broken!r}))\n")
        with pytest.raises(DriverError, match="schema"):
            run_client(spec, scenario_path=tmp_path / "s.json", base_url="http://x", token="t", timeout_s=10)

    def test_a_driver_that_says_nothing_is_refused(self, tmp_path: Path) -> None:
        spec = _script(tmp_path, "pass\n")
        with pytest.raises(DriverError, match="wrote nothing"):
            run_client(spec, scenario_path=tmp_path / "s.json", base_url="http://x", token="t", timeout_s=10)

    def test_the_result_is_the_last_line_so_logging_does_not_break_it(self, tmp_path: Path) -> None:
        spec = _script(tmp_path, f"import json\nprint('warming up')\nprint(json.dumps({_RESULT!r}))\n")
        assert (
            run_client(spec, scenario_path=tmp_path / "s.json", base_url="http://x", token="t", timeout_s=10)["status"]
            == "completed"
        )

    def test_a_client_that_never_finishes_is_killed(self, tmp_path: Path) -> None:
        spec = _script(tmp_path, "import time\ntime.sleep(60)\n")
        with pytest.raises(DriverError, match="timed out"):
            run_client(spec, scenario_path=tmp_path / "s.json", base_url="http://x", token="t", timeout_s=1)
