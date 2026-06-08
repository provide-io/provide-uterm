#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Playwright end-to-end coverage for the mounted browser terminal + WS/telnet proxy."""

from __future__ import annotations

import socketserver
import threading
import time
from typing import TYPE_CHECKING

import pytest
import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Page, expect
from provide.uterm.fastapi_utils import WsTerminalProxy, mount_terminal_ui
from provide.uterm.transports.telnet_server import _build_telnet_handshake

if TYPE_CHECKING:
    from collections.abc import Generator


class _ThreadedEchoServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], received_chunks: list[bytes]) -> None:
        self.received_chunks = received_chunks
        super().__init__(server_address, _EchoTelnetHandler)


class _EchoTelnetHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _ThreadedEchoServer)
        self.request.sendall(_build_telnet_handshake())
        self.request.sendall(b"WELCOME FROM TELNET\r\n")
        while True:
            data = self.request.recv(4096)
            if not data:
                return
            server.received_chunks.append(data)
            self.request.sendall(data)


@pytest.fixture(scope="session")
def terminal_proxy_server() -> Generator[tuple[str, list[bytes]], None, None]:
    received_chunks: list[bytes] = []
    telnet_server = _ThreadedEchoServer(("127.0.0.1", 0), received_chunks)
    telnet_thread = threading.Thread(target=telnet_server.serve_forever, daemon=True)
    telnet_thread.start()

    telnet_port = telnet_server.server_address[1]
    app = FastAPI()
    mount_terminal_ui(app)
    # terminal-page.js resolves to /ws/raw/{workerId}/term (default workerId="demo")
    app.include_router(WsTerminalProxy("127.0.0.1", telnet_port).create_router("/ws/raw/demo/term"))

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            telnet_server.shutdown()
            telnet_server.server_close()
            raise RuntimeError("terminal_proxy_server: uvicorn failed to start within 10 s")
        time.sleep(0.05)

    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", received_chunks

    server.should_exit = True
    thread.join(timeout=5)
    telnet_server.shutdown()
    telnet_server.server_close()
    telnet_thread.join(timeout=5)


class TestTerminalProxyPage:
    def test_terminal_page_round_trips_browser_input_through_ws_and_telnet(
        self,
        page: Page,
        terminal_proxy_server: tuple[str, list[bytes]],
    ) -> None:
        base_url, received_chunks = terminal_proxy_server
        page.goto(f"{base_url}/terminal/terminal.html", wait_until="domcontentloaded")

        expect(page.locator(".terminal-div")).to_be_visible(timeout=5000)
        expect(page.locator(".loading")).to_be_hidden(timeout=5000)
        page.wait_for_function("Boolean(window.demoTerminal)")

        page.evaluate("window.demoTerminal.handleTerminalInput('look\\r')")

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if b"look" in b"".join(received_chunks):
                break
            time.sleep(0.05)

        assert b"look" in b"".join(received_chunks)

        page.wait_for_function(
            "() => (window.__deepQuery('.terminal-div')?.textContent || '').includes('WELCOME FROM TELNET')"
        )
        terminal_text = page.locator(".terminal-div").text_content() or ""
        assert "WELCOME FROM TELNET" in terminal_text
        page.wait_for_function(
            "() => Boolean(window.demoTerminal && window.demoTerminal.getBufferText().includes('look'))"
        )

    def test_terminal_settings_are_applied_and_persist_across_reload(
        self,
        page: Page,
        terminal_proxy_server: tuple[str, list[bytes]],
    ) -> None:
        base_url, _received_chunks = terminal_proxy_server
        page.goto(f"{base_url}/terminal/terminal.html", wait_until="domcontentloaded")

        page.get_by_role("button", name="Settings").click()
        expect(page.locator(".settings-panel.open")).to_be_visible(timeout=5000)
        page.wait_for_function(
            "() => Boolean(window.demoTerminal && window.demoTerminal.getBufferText().includes('WELCOME FROM TELNET'))"
        )
        page.evaluate("window.__termRef = window.demoTerminal.term")

        page.get_by_role("button", name="BBS/DOS").click()
        page.get_by_role("button", name="Glass").click()
        page.locator("[id^='setFontSize-']").fill("16")
        page.locator("[id^='setFontSize-']").dispatch_event("input")
        page.locator("[id^='setPageBg-']").evaluate(
            "(el) => { el.value = '#112233'; el.dispatchEvent(new Event('input', { bubbles: true })); }"
        )
        page.locator("[id^='fxGlow-']").check()

        expect(page.locator(".provide-uterm.theme-glass")).to_be_visible(timeout=5000)
        expect(page.locator("[id^='valFontSize-']")).to_have_text("16px", timeout=5000)
        page.wait_for_function("window.__termRef === window.demoTerminal.term")
        page.wait_for_function("window.demoTerminal.getBufferText().includes('WELCOME FROM TELNET')")

        saved = page.evaluate("JSON.parse(localStorage.getItem('provide-uterm-settings'))")
        assert saved["theme"] == "glass"
        assert saved["fontSize"] == 16
        assert saved["pageBg"] == "#112233"
        assert saved["glow"] is True

        page.reload(wait_until="domcontentloaded")
        expect(page.locator(".provide-uterm.theme-glass")).to_be_visible(timeout=5000)
        expect(page.locator("[id^='valFontSize-']")).to_have_text("16px", timeout=5000)
        reloaded = page.evaluate("JSON.parse(localStorage.getItem('provide-uterm-settings'))")
        assert reloaded["theme"] == "glass"
        assert reloaded["fontSize"] == 16

    def test_terminal_reconnects_after_socket_close(
        self,
        page: Page,
        terminal_proxy_server: tuple[str, list[bytes]],
    ) -> None:
        base_url, _received_chunks = terminal_proxy_server
        page.goto(f"{base_url}/terminal/terminal.html", wait_until="domcontentloaded")

        expect(page.locator(".terminal-div")).to_be_visible(timeout=5000)
        page.wait_for_function("Boolean(window.demoTerminal)")
        expect(page.locator("[data-status-text='1']").first).to_have_text("Connected", timeout=5000)

        page.evaluate("window.demoTerminal.ws.close()")
        expect(page.locator("[data-status-text='1']").first).to_have_text("Disconnected", timeout=5000)
        expect(page.locator("[data-status-text='1']").first).to_have_text("Connected", timeout=5000)
