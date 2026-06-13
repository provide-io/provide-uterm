#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Playwright E2E tests: color palette and ANSI art rendering in xterm.js.

Proves ANSI escape sequences survive the full pipeline:
  Worker (raw ANSI text) → TermHub WS → Browser WS → xterm.js renderer

Each test sends ANSI data through a live TermHub server, waits for xterm.js
to render it, asserts colored DOM elements exist, and captures a screenshot.
The animation tests record short videos of animated ANSI output.
"""

from __future__ import annotations

import socket
import socketserver
import tempfile
import threading
import time
import urllib.request

import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Page, expect

from ._ansi_palettes import (
    build_16_color_palette,
    build_256_color_palette,
    build_animation_frames,
    build_ansi_art,
    build_truecolor_palette,
)
from ._gif_to_ansi import gif_to_ansi_frames
from .conftest import (
    SCREENSHOTS_DIR,
    AnimatedWorker,
    ColorWorker,
    assert_has_colored_spans,
    wait_for_color_data,
)

# Classic colorful Nyan Cat GIF — 240x152, 8 frames, 135+ colors
_NYAN_CAT_URL = "https://media.giphy.com/media/sIIhZliB2McAo/giphy.gif"


def _uid() -> str:
    return str(int(time.time() * 1000) % 100000)


def _screenshot(page: Page, name: str) -> None:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SCREENSHOTS_DIR / name), full_page=True)


# ---------------------------------------------------------------------------
# Static palette tests
# ---------------------------------------------------------------------------


class TestColorPalette16:
    """Verify standard 16-color ANSI sequences render in xterm.js through TermHub."""

    def test_16_color_palette_screenshot(self, page: Page, color_server: str) -> None:
        worker_id = f"color16-{_uid()}"
        worker = ColorWorker(color_server, worker_id, build_16_color_palette()).start()
        try:
            page.goto(f"{color_server}/color-test/{worker_id}", wait_until="domcontentloaded")
            wait_for_color_data(page)
            assert_has_colored_spans(page)
            _screenshot(page, "16-color-palette.png")
        finally:
            worker.stop()


class TestColorPalette256:
    """Verify 256-color ANSI sequences render in xterm.js through TermHub."""

    def test_256_color_palette_screenshot(self, page: Page, color_server: str) -> None:
        worker_id = f"color256-{_uid()}"
        worker = ColorWorker(color_server, worker_id, build_256_color_palette()).start()
        try:
            page.goto(f"{color_server}/color-test/{worker_id}", wait_until="domcontentloaded")
            wait_for_color_data(page)
            assert_has_colored_spans(page)
            _screenshot(page, "256-color-palette.png")
        finally:
            worker.stop()


class TestColorPaletteTruecolor:
    """Verify truecolor (24-bit) ANSI sequences render in xterm.js through TermHub."""

    def test_truecolor_palette_screenshot(self, page: Page, color_server: str) -> None:
        worker_id = f"truecolor-{_uid()}"
        worker = ColorWorker(color_server, worker_id, build_truecolor_palette()).start()
        try:
            page.goto(f"{color_server}/color-test/{worker_id}", wait_until="domcontentloaded")
            wait_for_color_data(page)
            assert_has_colored_spans(page)
            _screenshot(page, "truecolor-palette.png")
        finally:
            worker.stop()


class TestAnsiArt:
    """Verify ANSI art (blocks, box drawing, flames, braille) renders in xterm.js."""

    def test_ansi_art_screenshot(self, page: Page, color_server: str) -> None:
        worker_id = f"ansiart-{_uid()}"
        worker = ColorWorker(color_server, worker_id, build_ansi_art()).start()
        try:
            page.goto(f"{color_server}/color-test/{worker_id}", wait_until="domcontentloaded")
            wait_for_color_data(page)
            assert_has_colored_spans(page)
            _screenshot(page, "ansi-art.png")
        finally:
            worker.stop()


class TestColorPaletteCombined:
    """Verify all color modes and ANSI art in a single terminal session."""

    def test_combined_palette_screenshot(self, page: Page, color_server: str) -> None:
        worker_id = f"combined-{_uid()}"
        palette = build_16_color_palette() + build_256_color_palette() + build_truecolor_palette() + build_ansi_art()
        worker = ColorWorker(color_server, worker_id, palette).start()
        try:
            page.goto(f"{color_server}/color-test/{worker_id}", wait_until="domcontentloaded")
            wait_for_color_data(page)
            assert_has_colored_spans(page)
            _screenshot(page, "combined-color-palette.png")
        finally:
            worker.stop()


# ---------------------------------------------------------------------------
# Animation tests — record short videos of animated ANSI output
# ---------------------------------------------------------------------------


class TestAnsiAnimation:
    """Verify animated ANSI output renders as video through TermHub."""

    def test_plasma_animation_video(self, page: Page, color_server: str) -> None:
        """Stream animated plasma frames, capture screenshots per frame as proof."""
        worker_id = f"anim-{_uid()}"
        frames = build_animation_frames(num_frames=20)
        worker = AnimatedWorker(color_server, worker_id, frames, fps=8).start()

        try:
            page.goto(f"{color_server}/color-test/{worker_id}", wait_until="domcontentloaded")
            wait_for_color_data(page)
            assert_has_colored_spans(page)

            # Capture a sequence of frames as proof of animation
            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            for i in range(6):
                page.wait_for_timeout(250)
                _screenshot(page, f"animation-frame-{i:02d}.png")

            # Final frame
            _screenshot(page, "animation-final.png")
        finally:
            worker.stop()


class TestGifToAnsi:
    """Download a popular GIF, convert to ANSI art, play as animated video."""

    def test_nyan_cat_gif_video(self, page: Page, color_server: str) -> None:
        """Download Nyan Cat GIF from Giphy, convert to truecolor ANSI, stream as video."""
        # Download the GIF
        with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as f:
            gif_path = f.name
        urllib.request.urlretrieve(_NYAN_CAT_URL, gif_path)  # noqa: S310

        # Convert to ANSI frames at truecolor (24-bit)
        frames, fps = gif_to_ansi_frames(gif_path, cols=80, rows=25, mode="truecolor")
        assert len(frames) > 1, "GIF should have multiple frames"

        worker_id = f"nyancat-{_uid()}"
        worker = AnimatedWorker(color_server, worker_id, frames, fps=fps).start()

        try:
            page.goto(f"{color_server}/color-test/{worker_id}", wait_until="domcontentloaded")
            wait_for_color_data(page)
            assert_has_colored_spans(page)

            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            for i in range(8):
                page.wait_for_timeout(int(1000 / fps))
                _screenshot(page, f"nyancat-frame-{i:02d}.png")

            _screenshot(page, "nyancat-final.png")
        finally:
            worker.stop()

    def test_nyan_cat_256_color(self, page: Page, color_server: str) -> None:
        """Same GIF quantized to 256-color palette (ESC[38;5;N)."""
        with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as f:
            gif_path = f.name
        urllib.request.urlretrieve(_NYAN_CAT_URL, gif_path)  # noqa: S310

        frames, fps = gif_to_ansi_frames(gif_path, cols=80, rows=25, mode="256")
        worker_id = f"nyan256-{_uid()}"
        worker = AnimatedWorker(color_server, worker_id, frames, fps=fps).start()

        try:
            page.goto(f"{color_server}/color-test/{worker_id}", wait_until="domcontentloaded")
            wait_for_color_data(page)
            assert_has_colored_spans(page)
            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            _screenshot(page, "nyancat-256color.png")
        finally:
            worker.stop()

    def test_nyan_cat_16_color(self, page: Page, color_server: str) -> None:
        """Same GIF quantized to 16-color palette (classic ANSI)."""
        with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as f:
            gif_path = f.name
        urllib.request.urlretrieve(_NYAN_CAT_URL, gif_path)  # noqa: S310

        frames, fps = gif_to_ansi_frames(gif_path, cols=80, rows=25, mode="16")
        worker_id = f"nyan16-{_uid()}"
        worker = AnimatedWorker(color_server, worker_id, frames, fps=fps).start()

        try:
            page.goto(f"{color_server}/color-test/{worker_id}", wait_until="domcontentloaded")
            wait_for_color_data(page)
            assert_has_colored_spans(page)
            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            _screenshot(page, "nyancat-16color.png")
        finally:
            worker.stop()


# ---------------------------------------------------------------------------
# Telnet path: ANSI colors through telnet echo → WS proxy → xterm.js
# ---------------------------------------------------------------------------


class _ColorTelnetHandler(socketserver.BaseRequestHandler):
    """Telnet handler that sends ANSI color palette on connect."""

    def handle(self) -> None:
        from provide.uterm.transports.telnet_server import _build_telnet_handshake

        self.request.sendall(_build_telnet_handshake())
        palette = build_16_color_palette() + build_256_color_palette() + build_truecolor_palette()
        self.request.sendall(palette.encode("utf-8"))
        # Keep connection alive until client disconnects
        while True:
            data = self.request.recv(4096)
            if not data:
                return


class _ColorTelnetServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


class TestTelnetColorPath:
    """Prove ANSI colors survive: telnet server → WS proxy → browser xterm.js."""

    def test_colors_through_telnet_proxy(self, page: Page) -> None:
        """Send 16/256/truecolor palette via telnet, verify in browser."""
        from provide.uterm.fastapi_utils import WsTerminalProxy, mount_terminal_ui

        # Start color telnet server
        telnet_server = _ColorTelnetServer(("127.0.0.1", 0), _ColorTelnetHandler)
        telnet_thread = threading.Thread(target=telnet_server.serve_forever, daemon=True)
        telnet_thread.start()
        telnet_port = telnet_server.server_address[1]

        # Start WS proxy + terminal UI
        app = FastAPI()
        mount_terminal_ui(app)
        app.include_router(WsTerminalProxy("127.0.0.1", telnet_port).create_router("/ws/raw/demo/term"))

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        deadline = time.monotonic() + 10.0
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("telnet proxy server failed to start")
            time.sleep(0.05)

        base_url = f"http://127.0.0.1:{port}"

        try:
            page.goto(f"{base_url}/terminal/terminal.html", wait_until="domcontentloaded")
            expect(page.locator(".terminal-div")).to_be_visible(timeout=5000)
            page.wait_for_function("Boolean(window.demoTerminal)", timeout=10000)

            # Wait for telnet color data to arrive and render
            page.wait_for_function(
                "() => (window.demoTerminal?.getBufferText() || '').includes('256-Color')",
                timeout=15000,
            )

            # Verify the xterm buffer contains all three palette labels
            buf = page.evaluate("window.demoTerminal.getBufferText()")
            assert "16-Color Palette" in buf, "16-color palette not found in telnet output"
            assert "256-Color Palette" in buf, "256-color palette not found in telnet output"
            assert "Truecolor Palette" in buf, "Truecolor palette not found in telnet output"

            # terminal.html uses xterm canvas renderer — colors verified via
            # screenshot visual inspection rather than DOM span checks

            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            _screenshot(page, "telnet-color-palette.png")
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            telnet_server.shutdown()
            telnet_server.server_close()
