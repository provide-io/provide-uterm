#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Playwright regression: hijack widget honors server-confirmed role.

Fix #19 — the hijack widget now reads ``role`` from the server-confirmed
``hello`` frame (stashed on ``HijackState.serverRole``) and prefers it over the
constructor input for UX-mode decisions. An admin who didn't pass ``role:"admin"``
up front must still get the modal approval flow with Approve/Reject buttons.

The constructor here passes ``role: "viewer"`` while the server resolves the
browser to role ``"admin"``. A synthetic ``approval_pending`` frame is then
broadcast to the browser; the test asserts the **modal** approval UX is
rendered (including Approve/Reject buttons), not the statusbar UX.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import json
import socket
import threading
import time
import uuid
from collections.abc import Generator

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from playwright.sync_api import Page, expect
from provide.uterm.server.bridge.hub import TermHub
from starlette.staticfiles import StaticFiles


def _uid() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def admin_resolved_server() -> Generator[tuple[str, TermHub, asyncio.AbstractEventLoop], None, None]:
    """TermHub fixture that resolves every browser role to ``admin``.

    Same shape as the shared ``hijack_server`` fixture but with a distinct
    test page that hands the widget ``role: "viewer"`` in its constructor —
    the discrepancy is what proves the server-confirmed role wins. Yields
    the server loop too so tests can submit hub coroutines via
    ``asyncio.run_coroutine_threadsafe``.
    """
    hub = TermHub(
        resolve_browser_role=lambda _ws, _worker_id: "admin",
        browser_control_rate_limit_per_sec=1000,
    )
    app = FastAPI()
    app.include_router(hub.create_router())

    frontend_path = importlib.resources.files("provide.uterm.server") / "frontend"
    app.mount("/ui", StaticFiles(directory=str(frontend_path), html=True), name="ui")

    _XTERM_CDN = "https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0"
    _FIT_CDN = "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0"

    @app.get("/viewer-page/{worker_id}", response_class=HTMLResponse)
    async def viewer_page(worker_id: str) -> str:
        # Constructor passes role:"viewer" — server resolves to "admin".
        return f"""<!DOCTYPE html>
<html><head><meta charset='UTF-8'>
<link rel="stylesheet" href="{_XTERM_CDN}/css/xterm.css">
<style>*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:100%;height:100dvh;background:#0b0f14}}
#app{{width:100%;height:100%}}</style>
<script src="{_XTERM_CDN}/lib/xterm.js"></script>
<script src="{_FIT_CDN}/lib/addon-fit.js"></script>
</head>
<body><div id='app'></div>
<script type='module'>
import {{ ProvideHijack }} from '/ui/hijack.js';
window._widget = new ProvideHijack(document.getElementById('app'),
  {{workerId:{json.dumps(worker_id)},role:'viewer',heartbeatInterval:500}});
</script>
</body></html>"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
    server = uvicorn.Server(config)

    captured_loop: dict[str, asyncio.AbstractEventLoop] = {}

    def _run_server() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        captured_loop["loop"] = loop
        try:
            loop.run_until_complete(server.serve())
        finally:
            loop.close()

    thread = threading.Thread(target=_run_server, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("admin_resolved_server: uvicorn failed to start within 10 s")
        time.sleep(0.05)

    yield f"http://127.0.0.1:{port}", hub, captured_loop["loop"]

    server.should_exit = True
    thread.join(timeout=5)


class _MinimalWorker:
    """Connect to ``/ws/worker/{id}/term`` so the widget transitions out of Offline."""

    def __init__(self, base_url: str, worker_id: str) -> None:
        self._base_url = base_url
        self._worker_id = worker_id
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> _MinimalWorker:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._connected.wait(timeout=5.0):
            raise RuntimeError("_MinimalWorker: did not connect")
        return self

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._connect())
        finally:
            loop.close()

    async def _connect(self) -> None:
        import websockets

        from provide.uterm.control_channel import encode_control

        ws_url = self._base_url.replace("http://", "ws://") + f"/ws/worker/{self._worker_id}/term"
        try:
            async with websockets.connect(ws_url) as ws:
                self._connected.set()
                snapshot_msg = {
                    "type": "snapshot",
                    "screen": "",
                    "cursor": {"x": 0, "y": 0},
                    "cols": 80,
                    "rows": 25,
                    "screen_hash": "minimal",
                    "cursor_at_end": True,
                    "has_trailing_space": False,
                    "ts": time.time(),
                }
                await ws.send(encode_control(snapshot_msg))
                while not self._stop.is_set():
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=0.2)
                    except TimeoutError:
                        continue
                    except Exception:
                        break
        except Exception:
            self._connected.set()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)


def _broadcast_approval_pending(
    hub: TermHub, loop: asyncio.AbstractEventLoop, worker_id: str, command: str = "rm -rf /"
) -> str:
    """Push an ``approval_pending`` frame to every browser for *worker_id*.

    Schedules ``hub.broadcast`` on the server's event loop so the hub's
    ``asyncio.Lock`` (loop-bound) is acquired safely.
    """
    request_id = uuid.uuid4().hex

    async def _do() -> None:
        await hub.broadcast(
            worker_id,
            {
                "type": "approval_pending",
                "command": command,
                "request_id": request_id,
                "expires_at": time.time() + 60.0,
            },
        )

    fut = asyncio.run_coroutine_threadsafe(_do(), loop)
    fut.result(timeout=5.0)
    return request_id


class TestServerConfirmedRole:
    """Server-confirmed role > constructor role for UX mode decisions."""

    def test_admin_modal_renders_when_constructor_role_is_viewer(
        self,
        page: Page,
        admin_resolved_server: tuple[str, TermHub, asyncio.AbstractEventLoop],
    ) -> None:
        """ProvideHijack constructed with role:"viewer" still shows admin modal
        UI (with Approve/Reject buttons) when server confirms role "admin"."""
        base_url, hub, server_loop = admin_resolved_server
        worker_id = f"role-{_uid()}"
        worker = _MinimalWorker(base_url, worker_id).start()
        try:
            page.goto(f"{base_url}/viewer-page/{worker_id}", wait_until="domcontentloaded")

            # Wait for the widget to transition out of "Connecting…".
            page.wait_for_function(
                "() => { const t = document.querySelector('[id$=\"-statustext\"]');"
                " return t && t.textContent !== 'Connecting…'; }",
                timeout=10000,
            )

            # Confirm the server-confirmed role landed on HijackState.
            server_role = page.evaluate("() => window._widget?._state?.serverRole ?? null")
            assert server_role == "admin", (
                f"Server-confirmed role did not propagate to HijackState; got {server_role!r}"
            )
            # And the constructor role is still 'viewer' (proves the test is meaningful).
            constructor_role = page.evaluate("() => window._widget?._config?.role ?? null")
            assert constructor_role == "viewer", f"Constructor role should be 'viewer'; got {constructor_role!r}"

            # Trigger approval_pending via the hub. Because effective role is
            # 'admin', the widget must render the modal UX with action buttons.
            _broadcast_approval_pending(hub, server_loop, worker_id, command="rm -rf /tmp/danger")

            # Modal element class is "hijack-approval-modal".
            modal = page.locator(".hijack-approval-modal")
            expect(modal).to_be_visible(timeout=5000)
            # The Approve/Reject buttons are only rendered for admin.
            expect(modal.get_by_role("button", name="Approve")).to_be_visible()
            expect(modal.get_by_role("button", name="Reject")).to_be_visible()
            # And the statusbar UX (the alternative) must NOT be rendered.
            assert page.locator(".hijack-approval-statusbar").count() == 0
        finally:
            worker.stop()
