#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Live integration tests for the hosted terminal server app."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import asyncssh
import httpx
import websockets

from provide.uterm.transports.ssh import start_ssh_server

from .test_app_part1 import (  # noqa: F401
    _delete_session,
    _drain_snapshot_containing,
    _drain_until,
    _ws_url,
    live_reference_server,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestReferenceServerApp:
    async def _wait_for_connected(self, base_url: str, session_id: str) -> None:
        async with httpx.AsyncClient(base_url=base_url) as http:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                resp = await http.get(f"/api/sessions/{session_id}")
                if resp.status_code == 200 and resp.json()["connected"] is True:
                    return
                await asyncio.sleep(0.1)
        raise AssertionError(f"session did not become connected: {session_id}")

    async def test_api_lists_demo_session_and_pages_load(self, live_reference_server: str) -> None:
        async with httpx.AsyncClient(base_url=live_reference_server) as http:
            health = await http.get("/api/health")
            assert health.status_code == 200
            assert health.json()["ok"] is True
            assert health.headers.get("x-request-id")

            metrics = await http.get("/api/metrics")
            assert metrics.status_code == 200
            assert metrics.json()["metrics"]["http_requests_total"] >= 1

            sessions = await http.get("/api/sessions")
            assert sessions.status_code == 200
            payload = sessions.json()
            assert len(payload) == 1
            assert payload[0]["session_id"] == "provide-shell"

            dashboard = await http.get("/app/")
            assert dashboard.status_code == 200
            assert "id='app-root'" in dashboard.text
            assert '"page_kind": "dashboard"' in dashboard.text
            assert "<table" not in dashboard.text
            assert "addon-fit.js" in dashboard.text

            session_page = await http.get("/app/session/provide-shell")
            assert session_page.status_code == 200
            assert '"page_kind": "session"' in session_page.text
            assert "uterm_surface=user" in ",".join(session_page.headers.get_list("set-cookie"))
            assert "addon-fit.js" in session_page.text

            operator_page = await http.get("/app/operator/provide-shell")
            assert operator_page.status_code == 200
            assert '"page_kind": "operator"' in operator_page.text
            assert "uterm_surface=operator" in ",".join(operator_page.headers.get_list("set-cookie"))
            # Vite manifest entry (React app) required
            assert (
                "assets/main-" in operator_page.text or ".vite" in operator_page.text or "module" in operator_page.text
            )
            assert "<style>" not in operator_page.text
            assert "const SESSION_ID=" not in operator_page.text
            assert "btn-refresh" not in operator_page.text
            assert "addon-fit.js" in operator_page.text

            replay_page = await http.get("/app/replay/provide-shell")
            assert replay_page.status_code == 200
            assert "<style>" not in replay_page.text
            assert '"page_kind": "replay"' in replay_page.text
            assert "addon-fit.js" in replay_page.text

    async def test_demo_session_browser_ws_is_online(self, live_reference_server: str) -> None:
        await self._wait_for_connected(live_reference_server, "provide-shell")
        # The session API's ``connected`` flag only reflects connector state;
        # the hub may register the worker's WS slightly later. If the browser
        # WS opens before the worker is in the hub's ``_workers`` dict the
        # hello frame falls back to the default ``input_mode = "hijack"``.
        # Retry the WS open+hello read a few times until the hello reflects
        # the worker-registered state (or fail after a real deadline).
        deadline = time.monotonic() + 5.0
        hello: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            async with websockets.connect(_ws_url(live_reference_server, "/ws/browser/provide-shell/term")) as browser:
                hello = await _drain_until(browser, "hello", timeout=5.0)
            if hello is not None and hello.get("input_mode") == "open" and hello.get("worker_online") is True:
                break
            await asyncio.sleep(0.1)
        assert hello is not None
        assert hello["worker_online"] is True
        assert hello["input_mode"] == "open"
        assert hello["resume_supported"] is True
        assert isinstance(hello["resume_token"], str)
        assert len(hello["resume_token"]) > 10

    async def test_metrics_prometheus_endpoint(self, live_reference_server: str) -> None:
        async with httpx.AsyncClient(base_url=live_reference_server) as http:
            resp = await http.get("/api/metrics/prometheus")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers.get("content-type", "")
        body = resp.text
        assert "http_requests_total" in body
        lines = body.splitlines()
        type_lines = [ln for ln in lines if ln.startswith("# TYPE")]
        assert len(type_lines) > 0
        metric_lines = [ln for ln in lines if ln.strip() and not ln.startswith("#")]
        assert len(metric_lines) > 0
        for ln in metric_lines:
            parts = ln.split()
            assert len(parts) == 2, f"unexpected metric line: {ln!r}"
            assert parts[1].lstrip("-").isdigit(), f"non-integer value in: {ln!r}"

    async def test_metrics_include_ws_disconnect_and_hijack_counters(self, live_reference_server: str) -> None:
        await self._wait_for_connected(live_reference_server, "provide-shell")
        async with httpx.AsyncClient(base_url=live_reference_server) as http:
            before = (await http.get("/api/metrics")).json()["metrics"]
            base_disconnect = int(before.get("ws_disconnect_browser_total", 0))
            assert "hijack_conflicts_total" in before
            assert "hijack_lease_expiries_total" in before
        async with websockets.connect(_ws_url(live_reference_server, "/ws/browser/provide-shell/term")) as browser:
            assert await _drain_until(browser, "hello", timeout=5.0) is not None
        await asyncio.sleep(0.15)
        async with httpx.AsyncClient(base_url=live_reference_server) as http:
            after = (await http.get("/api/metrics")).json()["metrics"]
            assert int(after.get("ws_disconnect_browser_total", 0)) >= base_disconnect + 1

    async def test_hijack_conflict_counter_increments_on_second_acquire(self, live_reference_server: str) -> None:
        await self._wait_for_connected(live_reference_server, "provide-shell")
        async with httpx.AsyncClient(base_url=live_reference_server) as http:
            # Default session is open mode; switch to hijack so REST acquire works.
            # Restore in a try/finally so test-order randomization (pytest-randomly)
            # doesn't leak hijack mode into sibling tests that assert open mode.
            mode_resp = await http.post("/api/sessions/provide-shell/mode", json={"input_mode": "hijack"})
            assert mode_resp.status_code == 200
            try:
                before = (await http.get("/api/metrics")).json()["metrics"]
                base_conflicts = int(before.get("hijack_conflicts_total", 0))
                first = await http.post("/worker/provide-shell/hijack/acquire", json={"owner": "test-a", "lease_s": 60})
                assert first.status_code == 200
                second = await http.post(
                    "/worker/provide-shell/hijack/acquire", json={"owner": "test-b", "lease_s": 60}
                )
                assert second.status_code == 409
                hid = first.json()["hijack_id"]
                release = await http.post(f"/worker/provide-shell/hijack/{hid}/release")
                assert release.status_code == 200
                after = (await http.get("/api/metrics")).json()["metrics"]
                assert int(after.get("hijack_conflicts_total", 0)) >= base_conflicts + 1
            finally:
                await http.post("/api/sessions/provide-shell/mode", json={"input_mode": "open"})

    async def test_mode_changes_and_create_session_flow(self, live_reference_server: str) -> None:
        async with httpx.AsyncClient(base_url=live_reference_server) as http:
            created = await http.post(
                "/api/sessions",
                json={
                    "session_id": "scratch",
                    "display_name": "Scratch Demo",
                    "connector_type": "shell",
                    "input_mode": "hijack",
                    "auto_start": True,
                    "recording_enabled": True,
                },
            )
            assert created.status_code == 200
            assert created.json()["session_id"] == "scratch"
            await self._wait_for_connected(live_reference_server, "scratch")

            mode = await http.post("/api/sessions/scratch/mode", json={"input_mode": "open"})
            assert mode.status_code == 200
            assert mode.json()["input_mode"] == "open"

            analysis = await http.post("/api/sessions/scratch/analyze")
            assert analysis.status_code == 200
            assert "interactive shell analysis" in analysis.json()["analysis"]

            recording = await http.get("/api/sessions/scratch/recording")
            assert recording.status_code == 200
            assert recording.json()["enabled"] is True

            entries = await http.get("/api/sessions/scratch/recording/entries")
            assert entries.status_code == 200
            assert len(entries.json()) >= 1
            read_entries = await http.get("/api/sessions/scratch/recording/entries", params={"event": "read"})
            assert read_entries.status_code == 200
            assert read_entries.json()
            assert all(entry["event"] == "read" for entry in read_entries.json())

            paged_entries = await http.get(
                "/api/sessions/scratch/recording/entries",
                params={"event": "read", "limit": 1, "offset": 0},
            )
            assert paged_entries.status_code == 200
            assert len(paged_entries.json()) == 1
            assert paged_entries.json()[0]["event"] == "read"

            # Recording download can intermittently fail with protocol errors
            # when the server thread is starved during large test suites.
            # Use a fresh client with retries to avoid stale connection pooling.
            for attempt in range(5):
                try:
                    async with httpx.AsyncClient(base_url=live_reference_server, timeout=10.0) as dl_http:
                        download = await dl_http.get("/api/sessions/scratch/recording/download")
                        assert download.status_code == 200
                        assert "log_start" in download.text
                        break
                except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError, httpx.ReadTimeout):
                    if attempt == 4:
                        raise
                    await asyncio.sleep(0.5 * (attempt + 1))

    async def test_ssh_connector_can_host_a_local_shell(
        self, live_reference_server: str, free_port: int, tmp_path: Path
    ) -> None:
        async def _echo_handler(reader: Any, writer: Any) -> None:
            writer.write(b"welcome from ssh server\r\n")
            await writer.drain()
            while True:
                data = await reader.read(1024)
                if not data:
                    break
                writer.write(b"echo:" + data)
                await writer.drain()

        ssh_server = await start_ssh_server(
            _echo_handler,
            host="127.0.0.1",
            port=free_port,
            host_key_path=tmp_path,
        )
        try:
            async with httpx.AsyncClient(base_url=live_reference_server) as http:
                created = await http.post(
                    "/api/sessions",
                    json={
                        "session_id": "ssh-local",
                        "display_name": "Local SSH",
                        "connector_type": "ssh",
                        "auto_start": True,
                        "recording_enabled": False,
                        "connector_config": {
                            "host": "127.0.0.1",
                            "port": free_port,
                            "username": "tester",
                            "password": "uterm-test-secret-32-byte-minimum-key",
                            "insecure_no_host_check": True,
                        },
                    },
                )
                assert created.status_code == 200
                await self._wait_for_connected(live_reference_server, "ssh-local")

            async with websockets.connect(_ws_url(live_reference_server, "/ws/browser/ssh-local/term")) as browser:
                hello = await _drain_until(browser, "hello", timeout=5.0)
                assert hello is not None
                assert hello["worker_online"] is True
                snapshot = await _drain_until(browser, "snapshot", timeout=5.0)
                assert snapshot is not None
                assert "welcome from ssh server" in snapshot["screen"]
        finally:
            await _delete_session(live_reference_server, "ssh-local")
            ssh_server.close()
            await ssh_server.wait_closed()

    async def test_ssh_connector_rejects_client_key_path(
        self,
        live_reference_server: str,
        free_port: int,
        tmp_path: Path,
    ) -> None:
        async def _echo_handler(reader: Any, writer: Any) -> None:
            writer.write(b"welcome from key-backed ssh server\r\n")
            await writer.drain()
            while True:
                data = await reader.read(1024)
                if not data:
                    break
                writer.write(b"key-echo:" + data)
                await writer.drain()

        key_path = tmp_path / "client_key"
        key = asyncssh.generate_private_key("ssh-ed25519")
        key_path.write_bytes(key.export_private_key())
        key_path.chmod(0o600)

        ssh_server = await start_ssh_server(
            _echo_handler,
            host="127.0.0.1",
            port=free_port,
            host_key_path=tmp_path,
        )
        try:
            async with httpx.AsyncClient(base_url=live_reference_server) as http:
                created = await http.post(
                    "/api/sessions",
                    json={
                        "session_id": "ssh-key-local",
                        "display_name": "Local SSH Key",
                        "connector_type": "ssh",
                        "auto_start": True,
                        "recording_enabled": False,
                        "connector_config": {
                            "host": "127.0.0.1",
                            "port": free_port,
                            "username": "tester",
                            "client_key_path": str(key_path),
                            "insecure_no_host_check": True,
                        },
                    },
                )
                assert created.status_code == 422
                assert "client_key_path" in created.text
        finally:
            await _delete_session(live_reference_server, "ssh-key-local")
            ssh_server.close()
            await ssh_server.wait_closed()

    async def test_ssh_connector_can_use_inline_client_key_data(
        self,
        live_reference_server: str,
        free_port: int,
        tmp_path: Path,
    ) -> None:
        async def _echo_handler(reader: Any, writer: Any) -> None:
            writer.write(b"welcome from inline-key ssh server\r\n")
            await writer.drain()
            while True:
                data = await reader.read(1024)
                if not data:
                    break
                writer.write(b"inline-echo:" + data)
                await writer.drain()

        key = asyncssh.generate_private_key("ssh-ed25519")
        key_data = key.export_private_key()

        ssh_server = await start_ssh_server(
            _echo_handler,
            host="127.0.0.1",
            port=free_port,
            host_key_path=tmp_path,
        )
        try:
            async with httpx.AsyncClient(base_url=live_reference_server) as http:
                created = await http.post(
                    "/api/sessions",
                    json={
                        "session_id": "ssh-inline-key-local",
                        "display_name": "Local SSH Inline Key",
                        "connector_type": "ssh",
                        "auto_start": True,
                        "recording_enabled": False,
                        "connector_config": {
                            "host": "127.0.0.1",
                            "port": free_port,
                            "username": "tester",
                            "client_key_data": key_data.decode("utf-8"),
                            "insecure_no_host_check": True,
                        },
                    },
                )
                assert created.status_code == 200
                await self._wait_for_connected(live_reference_server, "ssh-inline-key-local")

            async with websockets.connect(
                _ws_url(live_reference_server, "/ws/browser/ssh-inline-key-local/term")
            ) as browser:
                hello = await _drain_until(browser, "hello", timeout=5.0)
                assert hello is not None
                assert hello["worker_online"] is True
                snapshot = await _drain_snapshot_containing(browser, "welcome from inline-key ssh server", timeout=10.0)
                assert snapshot is not None, "SSH echo greeting did not appear in any snapshot"
        finally:
            await _delete_session(live_reference_server, "ssh-inline-key-local")
            ssh_server.close()
            await ssh_server.wait_closed()


# (TestOnResumeCallback moved to test_app_2.py)
