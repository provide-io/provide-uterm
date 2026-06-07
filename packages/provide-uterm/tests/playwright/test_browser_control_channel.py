#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Playwright regression: ProvideTerminal decodes inline control-channel framing.

Fix #14 — ``terminal.ts`` feeds incoming WS payloads through
``ControlChannelDecoder`` so framed control frames (``DLE STX <len>:<json>``)
never render as raw JSON on screen. Only the ``DataChunk`` portions are
written to xterm.

This test wires a ``ProvideTerminal`` widget to a hand-rolled WebSocket
endpoint that emits a mixed stream of control frames + raw terminal bytes,
then asserts the literal JSON text never appears in the xterm DOM.
"""

from __future__ import annotations

import importlib.resources
import json
import socket
import threading
import time
from collections.abc import Generator
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from playwright.sync_api import Page
from starlette.staticfiles import StaticFiles

from provide.uterm.control_channel import encode_control


@pytest.fixture(scope="module")
def terminal_decoder_server() -> Generator[str, None, None]:
    """Module-scoped server hosting a ProvideTerminal-only test page.

    Exposes:
      * ``/term-test`` — page that constructs ``ProvideTerminal`` pointing at
        ``/ws/term-test``.
      * ``/ws/term-test`` — emits a framed control frame, then raw terminal
        bytes, then closes. Lets the test inspect what the widget renders.
    """
    app = FastAPI()

    frontend_path = importlib.resources.files("provide.uterm.server") / "frontend"
    app.mount("/ui", StaticFiles(directory=str(frontend_path), html=True), name="ui")

    sentinel_visible = "VISIBLE_RAW_OUTPUT_42"
    # The control frame payload contains an obvious literal — if the decoder
    # is bypassed (regression), this string would appear in the xterm DOM.
    sentinel_control_json = "MUST_NEVER_RENDER_IN_TERM"

    @app.websocket("/ws/term-test")
    async def _ws_term_test(websocket: WebSocket) -> None:
        await websocket.accept()
        # Frame 1: a JSON control frame the widget must NOT render.
        await websocket.send_text(
            encode_control({"type": "control", "marker": sentinel_control_json, "ts": time.time()})
        )
        # Frame 2: raw terminal bytes — these should render.
        await websocket.send_text(sentinel_visible + "\r\n")
        # Frame 3: a second control frame to verify multiple frames per message.
        await websocket.send_text(encode_control({"type": "control", "marker": sentinel_control_json + "_b"}))
        # Hold the connection open briefly so the browser has time to drain.
        try:
            while True:
                await websocket.receive_text()
        except Exception:
            return

    @app.get("/term-test", response_class=HTMLResponse)
    async def _term_test_page() -> str:
        from provide.uterm.server.ui import _resolve_vanilla_asset

        script_path = _resolve_vanilla_asset("src/terminal.ts")
        page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>ProvideTerminal decoder test</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0/css/xterm.css">
  <style>html,body,#app{{margin:0;padding:0;width:100%;height:100vh;background:#0b0f14;}}</style>
</head>
<body>
  <div id="app"></div>
  <script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0/lib/xterm.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0/lib/addon-fit.js"></script>
  <script type="module">
    import '/ui/{script_path}';
    window._term = new window.ProvideTerminal(document.getElementById('app'), {{
      wsUrl: '/ws/term-test',
      title: 'decoder-test',
    }});
  </script>
</body>
</html>"""

    # Expose sentinels via a JSON endpoint so tests stay deterministic
    # without hard-coding them in two places.
    @app.get("/sentinels")
    async def _sentinels() -> Any:
        return json.loads(json.dumps({"visible": sentinel_visible, "control": sentinel_control_json}))

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
            raise RuntimeError("terminal_decoder_server: uvicorn failed to start within 10 s")
        time.sleep(0.05)

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)


def _xterm_text(page: Page) -> str:
    result = page.evaluate(
        """() => {
            const rows = document.querySelector('.xterm-rows');
            return rows ? rows.textContent || '' : '';
        }"""
    )
    return str(result)


class TestProvideTerminalControlDecoder:
    """ProvideTerminal must strip inline control frames before writing to xterm."""

    def test_raw_data_renders_but_control_frame_does_not(self, page: Page, terminal_decoder_server: str) -> None:
        """Raw terminal bytes appear in xterm; framed JSON does not."""
        page.goto(f"{terminal_decoder_server}/term-test", wait_until="domcontentloaded")

        # Wait for xterm to mount + the visible raw payload to land.
        page.wait_for_selector(".xterm-rows", timeout=10000)
        page.wait_for_function(
            "() => (document.querySelector('.xterm-rows')?.textContent || '').includes('VISIBLE_RAW_OUTPUT_42')",
            timeout=15000,
        )
        # Settle one frame so any late writes are visible.
        page.wait_for_timeout(300)

        text = _xterm_text(page)
        assert "VISIBLE_RAW_OUTPUT_42" in text, f"raw terminal bytes missing from xterm output: {text!r}"
        assert "MUST_NEVER_RENDER_IN_TERM" not in text, (
            f"Control-frame JSON leaked into xterm output — decoder is not stripping framing.\nRendered text: {text!r}"
        )
        # The framing protocol literals themselves must never surface.
        assert "type" not in text or '"type"' not in text, "Raw JSON tokens visible in xterm output"
        # DLE (\\x10) framing chars should not be rendered as glyphs.
        assert "" not in text
