# SPDX-FileCopyrightText: Copyright (c) 2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Playwright proxy test: two browser windows connected to the same CF DO session.

Verifies the core proxy claim: a raw worker WS sends a snapshot to the CF DO,
and multiple browser connections to the same DO all receive the same terminal
output (snapshot broadcast).

Each browser opens a minimal status page (dark background, green text) that
shows received frames live — so when run with --headed you can watch both
windows receive the same snapshot in real time.

Run headed (visible windows) against real CF:
    REAL_CF=1 REAL_CF_URL=https://provide-terminal-cloudflare.neurotic.workers.dev \\
        uv run pytest tests/test_e2e_playwright_proxy.py -v --headed -s

Run headless (CI):
    REAL_CF=1 REAL_CF_URL=https://... uv run pytest tests/test_e2e_playwright_proxy.py -v

Markers:
    @e2e      — needs wrangler_server fixture (pywrangler dev or REAL_CF_URL)
    @real_cf  — needs real CF: webSocketOpen snapshot replay + WS server push
    @playwright — Playwright browser test (needs: playwright install)
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from typing import Any

import pytest
import websockets as _websockets
from playwright.sync_api import Page

_DEV_BEARER = "e2e-dev-token"

# JS injected into each page via a single evaluate() call.
# Using evaluate() (not set_content + evaluate) avoids the Playwright
# navigation-context reset that silently prevents the WS from being created.
# The script also writes a minimal visual status page so --headed runs show
# live frame arrival in both browser windows.
_OPEN_WS_JS = """\
(wsUrl) => {
    // Visual status page — use innerHTML, NOT document.write().
    // document.write() triggers a Chromium navigation event which resets
    // Playwright's wait_for_function polling context.
    document.body.style.cssText =
        'background:#0a0a0a;color:#33ff33;font-family:monospace;padding:20px;margin:0';
    document.body.innerHTML =
        '<h2 id=\"status\" style=\"color:#888;margin:0 0 8px;font-size:14px\">Connecting\u2026</h2>' +
        '<pre id=\"out\" style=\"background:#111;border:1px solid #333;padding:12px;' +
        'white-space:pre-wrap;word-break:break-all;font-size:12px;max-height:80vh;' +
        'overflow-y:auto\">(waiting for frames)</pre>';

    window.__wsFrames = [];
    // Strip control-channel framing: DLE(0x10) STX(0x02) {8 hex chars}:{json}
    function decodeFrame(raw) {
        if (raw.charCodeAt(0) === 0x10 && raw.charCodeAt(1) === 0x02) {
            var colon = raw.indexOf(':', 2);
            if (colon !== -1) return raw.slice(colon + 1);
        }
        return raw;
    }
    var ws = new WebSocket(wsUrl);
    ws.onopen = function() {
        document.getElementById('status').textContent = 'WS open \u2014 waiting for snapshot\u2026';
    };
    ws.onmessage = function(e) {
        if (typeof e.data !== 'string') return;
        var raw = decodeFrame(e.data);
        window.__wsFrames.push(raw);
        try {
            var m = JSON.parse(raw);
            var out = document.getElementById('out');
            if (m.type === 'snapshot') {
                document.getElementById('status').textContent =
                    'SNAPSHOT received (' + (m.screen || '').length + ' chars)';
                out.textContent = (m.screen || '').slice(0, 2000);
            } else if (out.textContent === '(waiting for frames)') {
                out.textContent = JSON.stringify(m, null, 2);
            } else {
                out.textContent += '\\n--- ' + m.type + ' ---';
            }
        } catch(e2) { /* non-JSON ignored */ }
    };
    ws.onclose = function() {
        var el = document.getElementById('status');
        if (el) el.textContent = 'WS closed';
    };
}
"""


def _ws_url(http_base: str, worker_id: str) -> str:
    base = http_base.replace("https://", "wss://").replace("http://", "ws://")
    return f"{base}/ws/browser/{worker_id}/term"


def _worker_ws_url(http_base: str, worker_id: str) -> str:
    base = http_base.replace("https://", "wss://").replace("http://", "ws://")
    return f"{base}/ws/worker/{worker_id}/term"


# ---------------------------------------------------------------------------
# JS poll helpers
# ---------------------------------------------------------------------------

_HELLO_POLL_JS = """\
() => (window.__wsFrames || []).some(f => {
    try { return JSON.parse(f).type === 'hello'; }
    catch(e) { return false; }
})
"""

_SNAPSHOT_POLL_JS = """\
() => (window.__wsFrames || []).some(f => {
    try { var m = JSON.parse(f); return m.type === 'snapshot' && typeof m.screen === 'string'; }
    catch(e) { return false; }
})
"""

_GET_SNAPSHOT_SCREEN_JS = """\
() => {
    for (var i = 0; i < (window.__wsFrames || []).length; i++) {
        try {
            var m = JSON.parse(window.__wsFrames[i]);
            if (m.type === 'snapshot' && typeof m.screen === 'string') return m.screen;
        } catch(e) {}
    }
    return null;
}
"""


def _open_ws_on_page(page: Page, ws_url: str) -> None:
    """Write a minimal status page and open a WS — all in one evaluate() call."""
    page.evaluate(_OPEN_WS_JS, ws_url)


# ---------------------------------------------------------------------------
# Background worker WS helper
# ---------------------------------------------------------------------------


def _send_snapshot_via_worker_ws(worker_ws_url: str, snapshot_screen: str, keep_alive_s: float = 5.0) -> None:
    """Open a raw worker WS, send a snapshot, keep alive, then close.

    Runs in a background thread so Playwright can poll concurrently.
    """

    async def _run() -> None:
        async with _websockets.connect(
            worker_ws_url,
            additional_headers={"Authorization": f"Bearer {_DEV_BEARER}"},
        ) as ws:
            await ws.send(json.dumps({"type": "snapshot", "screen": snapshot_screen, "ts": time.time()}))
            await asyncio.sleep(keep_alive_s)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.real_cf
@pytest.mark.playwright
def test_two_playwright_browsers_proxy(page: Page, browser: Any, wrangler_server: str) -> None:
    """Two browser windows connect to the same CF DO and both receive the same snapshot.

    With --headed you can watch both windows populate with terminal content simultaneously.

    Flow:
      1. Both browsers open WS to /ws/browser/{id}/term and wait for 'hello'.
      2. Background thread: raw worker WS connects and sends a specific snapshot string.
      3. DO's broadcast_to_browsers fires while both browser sockets are live.
      4. Assert both received an identical snapshot.screen value matching the sent string.

    This "browsers first, worker second" ordering ensures all WS connections are
    live simultaneously when the snapshot fires, which is required for CF DO's
    ctx.getWebSockets() to include both browser sockets during the broadcast wakeup.
    """
    worker_id = f"e2e-pw-{uuid.uuid4().hex[:8]}"
    snapshot_screen = f"playwright-proxy-{uuid.uuid4().hex[:8]}"
    browser_ws_url = _ws_url(wrangler_server, worker_id)
    worker_url = _worker_ws_url(wrangler_server, worker_id)

    # Browser B: separate context (isolated cookies/storage = genuinely separate client).
    ctx2 = browser.new_context()
    page2 = ctx2.new_page()

    try:
        # Step 1: Connect both browsers first so they are live when snapshot fires.
        _open_ws_on_page(page, browser_ws_url)
        _open_ws_on_page(page2, browser_ws_url)

        # Wait for hello on both — confirms WS is accepted and DO has registered the sockets.
        page.wait_for_function(_HELLO_POLL_JS, timeout=15_000)
        page2.wait_for_function(_HELLO_POLL_JS, timeout=15_000)

        # Step 2: Open worker WS and send snapshot in a background thread.
        # Both browser WS connections are already live, so the DO's broadcast_to_browsers
        # will find them via ctx.getWebSockets() in the same wakeup as the snapshot message.
        worker_thread = threading.Thread(
            target=_send_snapshot_via_worker_ws,
            args=(worker_url, snapshot_screen),
            kwargs={"keep_alive_s": 8.0},
            daemon=True,
        )
        worker_thread.start()

        # Step 3: Both browsers should receive the snapshot frame.
        page.wait_for_function(_SNAPSHOT_POLL_JS, timeout=30_000)
        page2.wait_for_function(_SNAPSHOT_POLL_JS, timeout=30_000)

        screen_a: str | None = page.evaluate(_GET_SNAPSHOT_SCREEN_JS)
        screen_b: str | None = page2.evaluate(_GET_SNAPSHOT_SCREEN_JS)

        assert screen_a is not None, "browser A received no snapshot"
        assert screen_b is not None, "browser B received no snapshot"
        assert screen_a == snapshot_screen, (
            f"browser A screen mismatch: expected={snapshot_screen!r} got={screen_a[:200]!r}"
        )
        assert screen_b == snapshot_screen, (
            f"browser B screen mismatch: expected={snapshot_screen!r} got={screen_b[:200]!r}"
        )
        assert screen_a == screen_b, (
            f"Proxy mismatch — browsers saw different snapshots:\n"
            f"  A ({len(screen_a)} chars): {screen_a[:200]!r}\n"
            f"  B ({len(screen_b)} chars): {screen_b[:200]!r}"
        )
    finally:
        worker_thread.join(timeout=15)
        page2.close()
        ctx2.close()
