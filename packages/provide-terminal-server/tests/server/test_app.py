#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Live integration tests for the hosted terminal server app."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

import asyncssh
import httpx
import pytest
import uvicorn
import websockets

from provide.terminal.control_channel import ControlChannelDecoder, ControlChunk, DataChunk
from provide.terminal.server import create_server_app, default_server_config
from provide.terminal.transports.ssh import start_ssh_server

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


def _ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http://", "ws://") + path


_WS_DECODERS: WeakKeyDictionary[Any, ControlChannelDecoder] = WeakKeyDictionary()
_WS_PENDING: WeakKeyDictionary[Any, list[dict[str, Any]]] = WeakKeyDictionary()


async def _drain_until(ws: Any, type_: str, timeout: float = 3.0) -> dict[str, Any] | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            pending = _WS_PENDING.setdefault(ws, [])
            if pending:
                msg = pending.pop(0)
                if msg.get("type") == type_:
                    return msg
                continue
            raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
            decoder = _WS_DECODERS.setdefault(ws, ControlChannelDecoder())
            events = decoder.feed(raw)
            for event in events:
                if isinstance(event, ControlChunk):
                    pending.append(event.control)
                elif isinstance(event, DataChunk):
                    pending.append({"type": "term", "data": event.data})
            if not pending:
                continue
            msg = pending.pop(0)
            if msg.get("type") == type_:
                return msg
        except TimeoutError:
            continue
    return None


@pytest.fixture()
def live_reference_server() -> Generator[str, None, None]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    base_url = f"http://127.0.0.1:{port}"
    config = default_server_config()
    config.auth.mode = "dev"
    config.server.host = "127.0.0.1"
    config.server.port = port
    config.server.public_base_url = base_url
    app = create_server_app(config)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("reference server did not start")
        time.sleep(0.05)

    yield base_url

    server.should_exit = True
    thread.join(timeout=5)


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
            # Either Vite manifest entry (React app) or legacy vanilla entry points
            has_vite = "assets/main-" in operator_page.text
            if has_vite:
                assert "assets/main-" in operator_page.text
            else:
                assert "/_terminal/server-app-foundation.css" in operator_page.text
                assert "/_terminal/server-session-page.js" in operator_page.text
            assert "<style>" not in operator_page.text
            assert "const SESSION_ID=" not in operator_page.text
            assert "btn-refresh" not in operator_page.text
            assert "addon-fit.js" in operator_page.text

            replay_page = await http.get("/app/replay/provide-shell")
            assert replay_page.status_code == 200
            if has_vite:
                assert "assets/main-" in replay_page.text
            else:
                assert "/_terminal/server-replay-page.js" in replay_page.text
            assert "<style>" not in replay_page.text
            assert '"page_kind": "replay"' in replay_page.text
            assert "addon-fit.js" in replay_page.text

    async def test_demo_session_browser_ws_is_online(self, live_reference_server: str) -> None:
        await self._wait_for_connected(live_reference_server, "provide-shell")
        async with websockets.connect(_ws_url(live_reference_server, "/ws/browser/provide-shell/term")) as browser:
            hello = await _drain_until(browser, "hello", timeout=5.0)
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
            before = (await http.get("/api/metrics")).json()["metrics"]
            base_conflicts = int(before.get("hijack_conflicts_total", 0))
            first = await http.post("/worker/provide-shell/hijack/acquire", json={"owner": "test-a", "lease_s": 60})
            assert first.status_code == 200
            second = await http.post("/worker/provide-shell/hijack/acquire", json={"owner": "test-b", "lease_s": 60})
            assert second.status_code == 409
            hid = first.json()["hijack_id"]
            release = await http.post(f"/worker/provide-shell/hijack/{hid}/release")
            assert release.status_code == 200
            after = (await http.get("/api/metrics")).json()["metrics"]
            assert int(after.get("hijack_conflicts_total", 0)) >= base_conflicts + 1

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

            download = await http.get("/api/sessions/scratch/recording/download")
            assert download.status_code == 200
            assert "log_start" in download.text

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
                            "password": "secret",
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
            ssh_server.close()
            await ssh_server.wait_closed()

    async def test_ssh_connector_can_use_a_generated_client_key(
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
                assert created.status_code == 200
                await self._wait_for_connected(live_reference_server, "ssh-key-local")

            async with websockets.connect(_ws_url(live_reference_server, "/ws/browser/ssh-key-local/term")) as browser:
                hello = await _drain_until(browser, "hello", timeout=5.0)
                assert hello is not None
                assert hello["worker_online"] is True
                snapshot = await _drain_until(browser, "snapshot", timeout=5.0)
                assert snapshot is not None
                assert "welcome from key-backed ssh server" in snapshot["screen"]
        finally:
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
                snapshot = await _drain_until(browser, "snapshot", timeout=5.0)
                assert snapshot is not None
                assert "welcome from inline-key ssh server" in snapshot["screen"]
        finally:
            ssh_server.close()
            await ssh_server.wait_closed()


# (TestOnResumeCallback moved to test_app_2.py)
