#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Playwright regression: ANSI soft-reset escape on every snapshot apply.

Fix #5 — ``hijack.ts`` prepends ``\\x1b[!p\\x1b[2J\\x1b[H`` (DECSTR + ED + CUP)
to every snapshot ``screen`` payload before writing it to xterm. Without the
leading ESC bytes the literal characters ``[!p[2J[H`` are rendered as text on
the terminal — visible to the user and a regression of the soft-reset semantic.

This test routes a snapshot through a real ``TermHub`` and asserts the literal
"[!p" never appears in the xterm DOM after the screen content is rendered.

The shared ``hijack_server`` test-page intentionally omits xterm.js — its
existing tests check status/button state only. We add a custom page here
that loads xterm.js from CDN so we can inspect rendered terminal content.
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
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from playwright.sync_api import Page
from provide.uterm.bridge.hub import TermHub
from starlette.staticfiles import StaticFiles

from provide.uterm.control_channel import encode_control


def _uid() -> str:
    return uuid.uuid4().hex[:8]


# CDN URLs duplicated from playwright/conftest.py — keeping them local avoids
# importing private fixture internals.
_XTERM_CDN = "https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0"
_FIT_CDN = "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0"


@pytest.fixture(scope="module")
def xterm_hijack_server() -> Generator[tuple[str, TermHub], None, None]:
    """Module-scoped server like ``hijack_server`` but loads xterm.js.

    The shared session ``hijack_server`` fixture omits xterm.js so its tests
    run faster; we need a real xterm to assert on rendered terminal content.
    """
    hub = TermHub(
        resolve_browser_role=lambda _ws, _worker_id: "admin",
        browser_control_rate_limit_per_sec=1000,
    )
    app = FastAPI()
    app.include_router(hub.create_router())

    frontend_path = importlib.resources.files("provide.uterm.server") / "frontend"
    app.mount("/ui", StaticFiles(directory=str(frontend_path), html=True), name="ui")

    @app.get("/xterm-test-page/{worker_id}", response_class=HTMLResponse)
    async def test_page(worker_id: str) -> str:
        return f"""<!DOCTYPE html>
<html><head><meta charset='UTF-8'>
<link rel="stylesheet" href="{_XTERM_CDN}/css/xterm.css">
<style>*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:100%;height:100dvh;background:#0b0f14}}
#app{{width:100%;height:100%}}</style>
<script src="{_XTERM_CDN}/lib/xterm.js"></script>
<script src="{_FIT_CDN}/lib/addon-fit.js"></script>
</head>
<body>
<div id='app'></div>
<script type='module'>
import {{ ProvideHijack }} from '/ui/hijack.js';
window._widget = new ProvideHijack(document.getElementById('app'),
  {{workerId:{json.dumps(worker_id)},heartbeatInterval:500}});
</script>
</body></html>"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("xterm_hijack_server: uvicorn failed to start within 10 s")
        time.sleep(0.05)

    yield f"http://127.0.0.1:{port}", hub

    server.should_exit = True
    thread.join(timeout=5)


class _SnapshotWorker:
    """Background-thread worker that connects and can push extra snapshots."""

    def __init__(self, base_url: str, worker_id: str, initial_screen: str) -> None:
        self._base_url = base_url
        self._worker_id = worker_id
        self._initial_screen = initial_screen
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> _SnapshotWorker:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._connected.wait(timeout=10.0):
            raise RuntimeError(f"_SnapshotWorker: {self._worker_id!r} did not connect")
        return self

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect())
        finally:
            self._loop.close()

    async def _connect(self) -> None:
        import websockets

        ws_url = self._base_url.replace("http://", "ws://") + f"/ws/worker/{self._worker_id}/term"
        try:
            async with websockets.connect(ws_url) as ws:
                self._ws = ws
                snapshot_msg = {
                    "type": "snapshot",
                    "screen": self._initial_screen,
                    "cursor": {"x": 0, "y": 0},
                    "cols": 80,
                    "rows": 25,
                    "screen_hash": f"snap-{self._worker_id}",
                    "cursor_at_end": True,
                    "has_trailing_space": False,
                    "ts": time.time(),
                }
                await ws.send(encode_control(snapshot_msg))
                self._connected.set()
                while not self._stop.is_set():
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=0.2)
                    except TimeoutError:
                        continue
                    except Exception:
                        break
        except Exception:
            self._connected.set()

    def push_snapshot(self, screen: str) -> None:
        """Send a fresh snapshot frame from the worker side."""
        if self._ws is None or self._loop is None:
            raise RuntimeError("_SnapshotWorker: not connected")
        snapshot_msg = {
            "type": "snapshot",
            "screen": screen,
            "cursor": {"x": 0, "y": 0},
            "cols": 80,
            "rows": 25,
            "screen_hash": f"snap-push-{time.time_ns()}",
            "cursor_at_end": True,
            "has_trailing_space": False,
            "ts": time.time(),
        }
        future = asyncio.run_coroutine_threadsafe(self._ws.send(encode_control(snapshot_msg)), self._loop)
        future.result(timeout=2.0)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)


def _navigate(page: Page, base_url: str, worker_id: str) -> None:
    page.goto(f"{base_url}/xterm-test-page/{worker_id}", wait_until="domcontentloaded")


def _xterm_text(page: Page) -> str:
    result = page.evaluate(
        """() => {
            const rows = document.querySelector('.xterm-rows');
            return rows ? rows.textContent || '' : '';
        }"""
    )
    return str(result)


class TestSnapshotSoftReset:
    """Verify the snapshot soft-reset ESC sequence is honored end-to-end."""

    def test_snapshot_does_not_render_literal_escape_text(
        self, page: Page, xterm_hijack_server: tuple[str, TermHub]
    ) -> None:
        """The xterm DOM must not contain the literal '[!p' after a snapshot.

        The widget writes ``\\x1b[!p\\x1b[2J\\x1b[H`` before the snapshot screen.
        If the leading ESC bytes were stripped (regression), xterm would render
        the bracket sequence as visible text.
        """
        base_url, _hub = xterm_hijack_server
        worker_id = f"snap-noesctxt-{_uid()}"
        screen = f"HELLO_SNAPSHOT_{worker_id}"
        worker = _SnapshotWorker(base_url, worker_id, initial_screen=screen).start()
        try:
            _navigate(page, base_url, worker_id)
            page.wait_for_function(
                "() => { const t = document.querySelector('[id$=\"-statustext\"]');"
                " return t && t.textContent === 'Connected (watching)'; }",
                timeout=10000,
            )
            page.wait_for_selector(".xterm-rows", timeout=10000)
            page.wait_for_function(
                f"() => (document.querySelector('.xterm-rows')?.textContent || '').includes({screen!r})",
                timeout=10000,
            )

            text = _xterm_text(page)
            assert "[!p" not in text, (
                f"Literal '[!p' appeared in xterm text — snapshot ESC bytes were not honored.\nRendered text: {text!r}"
            )
            assert "[2J" not in text, "Literal '[2J' appeared in xterm — ESC bytes not honored"
        finally:
            worker.stop()

    def test_subsequent_snapshot_clears_prior_content(
        self, page: Page, xterm_hijack_server: tuple[str, TermHub]
    ) -> None:
        """A second snapshot's soft-reset must wipe the first snapshot's content."""
        base_url, _hub = xterm_hijack_server
        worker_id = f"snap-clear-{_uid()}"
        first = f"FIRST_SNAPSHOT_{_uid()}"
        second = f"SECOND_SNAPSHOT_{_uid()}"
        worker = _SnapshotWorker(base_url, worker_id, initial_screen=first).start()
        try:
            _navigate(page, base_url, worker_id)
            page.wait_for_function(
                "() => { const t = document.querySelector('[id$=\"-statustext\"]');"
                " return t && t.textContent === 'Connected (watching)'; }",
                timeout=10000,
            )
            page.wait_for_selector(".xterm-rows", timeout=10000)
            page.wait_for_function(
                f"() => (document.querySelector('.xterm-rows')?.textContent || '').includes({first!r})",
                timeout=10000,
            )

            worker.push_snapshot(second)

            page.wait_for_function(
                f"() => (document.querySelector('.xterm-rows')?.textContent || '').includes({second!r})",
                timeout=10000,
            )
            page.wait_for_timeout(200)
            text = _xterm_text(page)
            assert second in text
            assert first not in text, f"Prior snapshot text was not cleared by soft-reset.\nRendered text: {text!r}"
            assert "[!p" not in text
        finally:
            worker.stop()
