#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Playwright tests proving the reconnect-nudge and spinner animation feature.

Two behaviours are exercised end-to-end:

1. **Nudge reconnect** — a keypress while the WS is in backoff cancels the
   pending timer and calls _connectWs() immediately.  We force a WS close,
   wait until the widget enters "Reconnecting in 1s…", then fire the onData
   callback and verify the widget re-connects in well under 1 s.

2. **Spinner ANSI** — while the backoff timer is active, _startReconnectAnim
   fires setInterval(80 ms) writing ``\\x1b7…\\x1b8`` save/restore sequences
   with a braille frame.  After reconnect, _stopReconnectAnim writes the
   erase sequence and clears the interval.

A mock ``window.Terminal`` / ``window.FitAddon`` is injected via the test
page HTML so ``_ensureTerm()`` succeeds without a CDN load.  The widget
instance is exposed as ``window._widget``; the onData callback as
``window._onDataCb``; all ``term.write()`` calls are accumulated in
``window._termWrites``.
"""

from __future__ import annotations

import importlib.resources
import json
import threading
import time
import uuid
from typing import TYPE_CHECKING

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from playwright.sync_api import Page

from provide.terminal.bridge.hub import TermHub

if TYPE_CHECKING:
    from collections.abc import Generator


def _uid() -> str:
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Session-scoped server with mock-Terminal page
# ---------------------------------------------------------------------------

_MOCK_TERMINAL_JS = """
<script>
// Mock xterm Terminal — records writes so tests can inspect them.
window._termWrites = [];
window.Terminal = class MockTerminal {
  constructor(opts) { this._onDataCb = null; }
  open(el) {}
  focus() {}
  write(data) { window._termWrites.push(data); }
  reset() { window._termWrites.push('\x00RESET\x00'); }
  loadAddon(addon) {}
  onData(cb) { this._onDataCb = cb; window._onDataCb = cb; }
  dispose() {}
};
// Widget accesses window.FitAddon.FitAddon as the constructor
window.FitAddon = { FitAddon: class MockFitAddon { fit() {} } };
</script>
"""


@pytest.fixture(scope="session")
def spinner_server() -> Generator[tuple[str, TermHub], None, None]:
    """Session-scoped server whose test page injects a mock xterm Terminal."""
    from starlette.staticfiles import StaticFiles

    hub = TermHub(resolve_browser_role=lambda _ws, _worker_id: "admin")
    app = FastAPI()
    app.include_router(hub.create_router())

    frontend_path = importlib.resources.files("provide.terminal") / "frontend"
    app.mount("/ui", StaticFiles(directory=str(frontend_path), html=True), name="ui")

    @app.get("/test-page/{worker_id}", response_class=HTMLResponse)
    async def test_page(worker_id: str) -> str:
        return (
            "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
            "<style>*{margin:0;padding:0;box-sizing:border-box}"
            "html,body{width:100%;height:100dvh;background:#0b0f14}"
            "#app{width:100%;height:100%}</style></head>"
            "<body><div id='app'></div>"
            f"{_MOCK_TERMINAL_JS}"
            "<script type='module'>"
            "import { ProvideHijack } from '/ui/hijack.js';"
            "window._widget = new ProvideHijack(document.getElementById('app'),"
            f"{{workerId:{json.dumps(worker_id)},heartbeatInterval:500}});"
            "</script>"
            "</body></html>"
        )

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("spinner_server: uvicorn failed to start within 10 s")
        time.sleep(0.05)

    port: int = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", hub

    server.should_exit = True
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BRAILLE = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")


def _status(page: Page) -> str:
    return page.locator("[id$='-statustext']").text_content() or ""


def _navigate(page: Page, base_url: str, worker_id: str) -> None:
    page.goto(f"{base_url}/test-page/{worker_id}", wait_until="domcontentloaded")


def _wait_ws_open(page: Page, timeout: float = 5000) -> None:
    """Wait until the widget's WS is in OPEN state (readyState === 1)."""
    page.wait_for_function(
        "window._widget._ws !== null && window._widget._ws.readyState === 1",
        timeout=timeout,
    )


def _wait_reconnecting(page: Page, timeout: float = 3000) -> None:
    page.wait_for_function(
        "document.querySelector('[id$=\"-statustext\"]')?.textContent.includes('Reconnecting')",
        timeout=timeout,
    )


def _init_term(page: Page) -> None:
    """Force-initialise _term so onData is wired (lazy; only happens on first message otherwise)."""
    page.evaluate("window._widget._ensureTerm()")


def _fire_key(page: Page) -> None:
    """Simulate a keypress via the widget's onData callback (same code path as real xterm input)."""
    page.evaluate("if (window._onDataCb) window._onDataCb('a')")


def _force_close_ws(page: Page) -> None:
    """Close the browser-side WS socket so the widget enters reconnect mode."""
    page.evaluate("if (window._widget._ws) window._widget._ws.close()")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNudgeReconnect:
    @pytest.mark.playwright
    def test_keypress_cancels_backoff_timer(self, page: Page, spinner_server: tuple[str, TermHub]) -> None:
        """A keypress during backoff sets _reconnectTimer to null immediately."""
        base_url, _ = spinner_server
        worker_id = f"nudge-timer-{_uid()}"
        _navigate(page, base_url, worker_id)

        page.wait_for_function(
            "document.querySelector('[id$=\"-statustext\"]')?.textContent !== 'Connecting\u2026'",
            timeout=5000,
        )
        _init_term(page)
        _force_close_ws(page)
        _wait_reconnecting(page)

        has_timer_before = page.evaluate("window._widget._reconnectTimer !== null")
        assert has_timer_before, "reconnect timer must be set while in backoff"

        _fire_key(page)

        timer_after = page.evaluate("window._widget._reconnectTimer")
        assert timer_after is None, "_nudgeReconnect must clear the backoff timer"

    @pytest.mark.playwright
    def test_reconnect_completes_fast_after_keypress(self, page: Page, spinner_server: tuple[str, TermHub]) -> None:
        """Widget WS enters OPEN state in well under 1 s (the first backoff delay) after a keypress."""
        base_url, _ = spinner_server
        worker_id = f"nudge-speed-{_uid()}"
        _navigate(page, base_url, worker_id)

        page.wait_for_function(
            "document.querySelector('[id$=\"-statustext\"]')?.textContent !== 'Connecting\u2026'",
            timeout=5000,
        )
        _init_term(page)
        _force_close_ws(page)
        _wait_reconnecting(page)

        t0 = time.monotonic()
        _fire_key(page)
        _wait_ws_open(page, timeout=2000)
        elapsed = time.monotonic() - t0

        # First backoff delay is 1 s; nudged reconnect must complete well under that
        assert elapsed < 0.9, f"reconnect took {elapsed:.2f}s — nudge did not fire immediately"


class TestSpinnerAnim:
    @pytest.mark.playwright
    def test_spinner_writes_braille_ansi_frames(self, page: Page, spinner_server: tuple[str, TermHub]) -> None:
        """_startReconnectAnim writes DECSC/DECRC save-restore sequences with braille frames."""
        base_url, _ = spinner_server
        worker_id = f"spin-ansi-{_uid()}"
        _navigate(page, base_url, worker_id)

        page.wait_for_function(
            "document.querySelector('[id$=\"-statustext\"]')?.textContent !== 'Connecting\u2026'",
            timeout=5000,
        )
        _init_term(page)
        page.evaluate("window._termWrites.length = 0")

        # Start the spinner directly (no nudge — avoids immediate reconnect race)
        page.evaluate("window._widget._startReconnectAnim()")

        # Wait for at least two frames (interval is 80 ms → expect frames within 250 ms)
        page.wait_for_function(
            "window._termWrites.filter(w => w.charCodeAt(0) === 27 && w.includes('\\x1b8') && w.includes('\\x1b[2;36m')).length >= 2",
            timeout=500,
        )

        # Stop the animation so later tests start clean
        page.evaluate("window._widget._stopReconnectAnim()")

        writes = page.evaluate("window._termWrites")
        frame_writes = [w for w in writes if len(w) > 0 and ord(w[0]) == 27 and "\x1b[2;36m" in w]
        assert len(frame_writes) >= 2, f"expected ≥2 spinner frames; got: {writes!r}"

        # Every frame must contain a braille character and the dim-cyan colour code
        for fw in frame_writes:
            assert any(ch in fw for ch in _BRAILLE), f"no braille frame in: {fw!r}"
            assert "\x1b[2;36m" in fw
            # DECSC (\x1b7) and DECRC (\x1b8) save/restore must bracket the write
            assert fw.startswith("\x1b7"), f"expected DECSC prefix in: {fw!r}"
            assert fw.endswith("\x1b8"), f"expected DECRC suffix in: {fw!r}"

    @pytest.mark.playwright
    def test_spinner_stop_writes_erase_and_clears_timer(self, page: Page, spinner_server: tuple[str, TermHub]) -> None:
        """_stopReconnectAnim clears the interval and writes the space-erase sequence."""
        base_url, _ = spinner_server
        worker_id = f"spin-stop-{_uid()}"
        _navigate(page, base_url, worker_id)

        page.wait_for_function(
            "document.querySelector('[id$=\"-statustext\"]')?.textContent !== 'Connecting\u2026'",
            timeout=5000,
        )
        _init_term(page)

        # Start the spinner and wait for at least one frame
        page.evaluate("window._widget._startReconnectAnim()")
        page.wait_for_function(
            "window._termWrites.some(w => w.charCodeAt(0) === 27 && w.includes('\\x1b[2;36m'))",
            timeout=500,
        )

        assert page.evaluate("window._widget._reconnectAnimTimer !== null"), (
            "_reconnectAnimTimer must be non-null while running"
        )

        page.evaluate("window._termWrites.length = 0")
        page.evaluate("window._widget._stopReconnectAnim()")

        anim_timer = page.evaluate("window._widget._reconnectAnimTimer")
        assert anim_timer is None, "_reconnectAnimTimer must be null after stop"

        writes = page.evaluate("window._termWrites")
        # Erase write: \x1b7\x1b[B\x1b[G<SPACE>\x1b8  (space instead of braille, no colour code)
        erase_writes = [w for w in writes if len(w) > 0 and ord(w[0]) == 27 and " " in w and "\x1b8" in w]
        assert len(erase_writes) >= 1, f"expected spinner erase write; got: {writes!r}"
        assert erase_writes[0].startswith("\x1b7"), "erase write must start with DECSC"
        assert erase_writes[0].endswith("\x1b8"), "erase write must end with DECRC"

    @pytest.mark.playwright
    def test_spinner_fires_on_keypress_and_stops_on_reconnect(
        self, page: Page, spinner_server: tuple[str, TermHub]
    ) -> None:
        """Integration: keypress during backoff starts spinner; after reconnect it stops."""
        base_url, _ = spinner_server
        worker_id = f"spin-integ-{_uid()}"
        _navigate(page, base_url, worker_id)

        page.wait_for_function(
            "document.querySelector('[id$=\"-statustext\"]')?.textContent !== 'Connecting\u2026'",
            timeout=5000,
        )
        _init_term(page)

        # Force disconnect — widget enters backoff
        _force_close_ws(page)
        _wait_reconnecting(page)

        # Keypress fires _nudgeReconnect + _startReconnectAnim.
        # Read _reconnectAnimTimer in the same evaluate so we capture it before
        # ws.onopen can fire (onopen is async but runs in the same JS thread after
        # evaluate returns — still, same-call guarantees the read happens first).
        timer_set_during_keypress = page.evaluate("""() => {
          if (window._onDataCb) window._onDataCb('a');
          return window._widget._reconnectAnimTimer !== null;
        }""")
        assert timer_set_during_keypress, "_startReconnectAnim must set _reconnectAnimTimer immediately on keypress"

        # Reconnect completes (nudge fires _connectWs; server is local → fast)
        _wait_ws_open(page, timeout=2000)

        # Give onopen a tick to call _stopReconnectAnim
        page.wait_for_function(
            "window._widget._reconnectAnimTimer === null",
            timeout=1000,
        )
        assert page.evaluate("window._widget._reconnectAnimTimer") is None, (
            "_reconnectAnimTimer must be cleared after WS reconnects"
        )
