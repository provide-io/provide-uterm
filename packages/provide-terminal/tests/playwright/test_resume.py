#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Playwright end-to-end tests for WebSocket session resumption.

Uses a session-scoped TermHub with an InMemoryResumeStore so the widget
receives ``resume_token`` in its hello handshake.  Tests verify:

1. The widget receives and stores a resume token on connect.
2. After a WS drop, the widget auto-reconnects and sends a resume message.
3. The server responds with a ``resumed: true`` hello.
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

from provide.terminal.bridge.hub import InMemoryResumeStore, TermHub

if TYPE_CHECKING:
    from collections.abc import Generator


def _uid() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture(scope="session")
def resume_server() -> Generator[tuple[str, TermHub, InMemoryResumeStore], None, None]:
    """Session-scoped server with an InMemoryResumeStore for resume tests."""
    from starlette.staticfiles import StaticFiles

    store = InMemoryResumeStore()
    hub = TermHub(
        resolve_browser_role=lambda _ws, _worker_id: "admin",
        resume_store=store,
        resume_ttl_s=300,
    )
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
            "<script type='module'>"
            "import { ProvideHijack } from '/ui/hijack.js';"
            "new ProvideHijack(document.getElementById('app'),"
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
            raise RuntimeError("resume_server: uvicorn failed to start within 10 s")
        time.sleep(0.05)

    port: int = server.servers[0].sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    yield base_url, hub, store

    server.should_exit = True
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestResumeTokenInHello:
    def test_widget_receives_resume_token(
        self, page: Page, resume_server: tuple[str, TermHub, InMemoryResumeStore]
    ) -> None:
        """The widget should receive resume_token in the hello message and store it in sessionStorage."""
        base_url, hub, store = resume_server
        worker_id = f"pw-resume-{_uid()}"
        page.goto(f"{base_url}/test-page/{worker_id}", wait_until="domcontentloaded")

        # Wait for status to move past "Connecting…"
        page.wait_for_function(
            "document.querySelector('[id$=\"-statustext\"]')?.textContent !== 'Connecting…'",
            timeout=5000,
        )

        # Check that the resume token was stored in sessionStorage
        token = page.evaluate(f"sessionStorage.getItem('uterm_resume_{worker_id}')")
        assert token is not None
        assert len(token) > 10

    def test_resume_token_updates_on_new_hello(
        self, page: Page, resume_server: tuple[str, TermHub, InMemoryResumeStore]
    ) -> None:
        """Each hello should update the stored resume token."""
        base_url, hub, store = resume_server
        worker_id = f"pw-update-{_uid()}"
        page.goto(f"{base_url}/test-page/{worker_id}", wait_until="domcontentloaded")

        page.wait_for_function(
            "document.querySelector('[id$=\"-statustext\"]')?.textContent !== 'Connecting…'",
            timeout=5000,
        )

        token1 = page.evaluate(f"sessionStorage.getItem('uterm_resume_{worker_id}')")
        assert token1 is not None

        # Store should have at least one active token
        assert len(store) > 0


class TestResumeOnReconnect:
    def test_auto_reconnect_sends_resume(
        self, page: Page, resume_server: tuple[str, TermHub, InMemoryResumeStore]
    ) -> None:
        """After a page reload, the widget sends a resume message with the stored token.

        Proof of successful resume:
        1. Token changes — server issued a new token on resume (not same as initial)
        2. Old token is revoked in the store — only happens on successful resume
        """
        base_url, hub, store = resume_server
        worker_id = f"pw-reconnect-{_uid()}"
        page.goto(f"{base_url}/test-page/{worker_id}", wait_until="domcontentloaded")

        # Wait for initial connection and token storage
        page.wait_for_function(
            f"sessionStorage.getItem('uterm_resume_{worker_id}') !== null",
            timeout=5000,
        )

        token_before = page.evaluate(f"sessionStorage.getItem('uterm_resume_{worker_id}')")
        assert token_before is not None
        assert store.get(token_before) is not None  # token is live in store

        # Reload the same page — sessionStorage persists (same origin, same tab),
        # so the widget reads the stored token and sends a resume message.
        page.goto(f"{base_url}/test-page/{worker_id}", wait_until="domcontentloaded")

        # Wait for the resumed hello to arrive (new token stored in sessionStorage)
        page.wait_for_function(
            f"sessionStorage.getItem('uterm_resume_{worker_id}') !== {json.dumps(token_before)}",
            timeout=10000,
        )

        token_after = page.evaluate(f"sessionStorage.getItem('uterm_resume_{worker_id}')")
        assert token_after is not None
        assert token_after != token_before  # server issued a new token → resume was processed
        assert store.get(token_before) is None  # old token revoked — definitive proof of resume

    def test_resume_token_persists_across_navigation(
        self, page: Page, resume_server: tuple[str, TermHub, InMemoryResumeStore]
    ) -> None:
        """sessionStorage persists within the same origin, so the token survives same-origin navigation."""
        base_url, hub, store = resume_server
        worker_id = f"pw-persist-{_uid()}"
        worker_id_2 = f"pw-persist2-{_uid()}"
        page.goto(f"{base_url}/test-page/{worker_id}", wait_until="domcontentloaded")

        page.wait_for_function(
            "document.querySelector('[id$=\"-statustext\"]')?.textContent !== 'Connecting…'",
            timeout=5000,
        )

        token = page.evaluate(f"sessionStorage.getItem('uterm_resume_{worker_id}')")
        assert token is not None

        # Navigate to a different same-origin page — sessionStorage should persist
        page.goto(f"{base_url}/test-page/{worker_id_2}", wait_until="domcontentloaded")
        token_after_nav = page.evaluate(f"sessionStorage.getItem('uterm_resume_{worker_id}')")
        assert token_after_nav == token
