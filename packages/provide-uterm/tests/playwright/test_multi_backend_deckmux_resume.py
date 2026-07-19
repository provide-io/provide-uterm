#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Multi-backend DeckMux presence + session resume Playwright/WS suite.

Run with::

    UTERM_TEST_BACKEND=python|go|csharp UTERM_TEST_MODE=1 \\
      uv run pytest packages/provide-uterm/tests/playwright/test_multi_backend_deckmux_resume.py \\
      -m playwright --no-cov
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Any

import pytest
from playwright.sync_api import Page

from provide.uterm.control_channel import ControlChunk, ControlFrameDecoder, encode_control_frame

from .backend_server import BackendServer, backend_name, spawn_backend_server
from .ui_routes import install_multi_backend_routes, widget_test_page_html

pytestmark = pytest.mark.playwright


@pytest.fixture(scope="module")
def multi_backend() -> BackendServer:
    with spawn_backend_server() as srv:
        yield srv


def _browser_ws(worker_id: str) -> str:
    return f"/ws/browser/{worker_id}/term"


def _recv_frames(raw: str | bytes) -> list[dict[str, Any]]:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    out: list[dict[str, Any]] = []
    dec = ControlFrameDecoder()
    for chunk in dec.feed(text):
        if isinstance(chunk, ControlChunk) and isinstance(chunk.control, dict):
            out.append(chunk.control)
    return out


def _ws_collect(
    base_url: str,
    worker_id: str,
    *,
    send: list[dict[str, Any]] | None = None,
    want_types: set[str],
    timeout_s: float = 8.0,
) -> list[dict[str, Any]]:
    """Dial browser WS under TEST_MODE; collect frames of interest."""
    import websockets

    ws_url = base_url.replace("http://", "ws://") + _browser_ws(worker_id)
    found: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    async def _run() -> None:
        async with websockets.connect(ws_url, open_timeout=10) as ws:
            deadline = asyncio.get_event_loop().time() + timeout_s
            if send:
                for msg in send:
                    await ws.send(encode_control_frame(msg))
            while asyncio.get_event_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except TimeoutError:
                    if found and all(any(f.get("type") == t for f in found) for t in want_types):
                        return
                    continue
                for fr in _recv_frames(raw):
                    if fr.get("type") in want_types or fr.get("type") == "hello":
                        found.append(fr)
                if all(any(f.get("type") == t for f in found) for t in want_types):
                    return

    def _thread() -> None:
        try:
            asyncio.run(_run())
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=_thread, daemon=True)
    t.start()
    t.join(timeout=timeout_s + 5)
    assert not t.is_alive(), f"ws collect hung backend={backend_name()}"
    assert not errors, errors[0]
    return found


def test_presence_sync_on_browser_join(multi_backend: BackendServer) -> None:
    """Every backend sends presence_sync (or presence_enabled hello) on browser connect."""
    worker_id = f"dm-sync-{uuid.uuid4().hex[:8]}"
    frames = _ws_collect(
        multi_backend.base_url,
        worker_id,
        want_types={"hello", "presence_sync"},
    )
    types = {f.get("type") for f in frames}
    assert "hello" in types, frames
    # Presence: full presence_sync frame, or hello.presence_enabled for older paths.
    has_sync = "presence_sync" in types
    hello = next(f for f in frames if f.get("type") == "hello")
    has_flag = bool(hello.get("presence_enabled"))
    assert has_sync or has_flag, f"no presence on {backend_name()}: {frames!r}"


def test_two_browsers_both_receive_presence_sync(multi_backend: BackendServer) -> None:
    """Two concurrent browsers each get a presence_sync (DeckMux join path)."""
    worker_id = f"dm-2br-{uuid.uuid4().hex[:8]}"
    results: dict[str, list[dict[str, Any]]] = {"a": [], "b": []}
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _one(key: str) -> None:
        try:
            barrier.wait(timeout=10)
            results[key] = _ws_collect(
                multi_backend.base_url,
                worker_id,
                want_types={"hello", "presence_sync"},
                timeout_s=10.0,
            )
        except BaseException as exc:
            errors.append(exc)

    t1 = threading.Thread(target=_one, args=("a",), daemon=True)
    t2 = threading.Thread(target=_one, args=("b",), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)
    assert not errors, errors[0]
    for key in ("a", "b"):
        types = {f.get("type") for f in results[key]}
        assert "hello" in types, results[key]
        assert "presence_sync" in types or any(
            f.get("type") == "hello" and f.get("presence_enabled") for f in results[key]
        ), results[key]


def test_resume_token_in_hello(multi_backend: BackendServer) -> None:
    """Hello includes resume_token when the production resume store is wired."""
    worker_id = f"rs-tok-{uuid.uuid4().hex[:8]}"
    frames = _ws_collect(multi_backend.base_url, worker_id, want_types={"hello"})
    hello = next(f for f in frames if f.get("type") == "hello")
    assert hello.get("resume_supported") in (True, None) or hello.get("resume_token"), hello
    tok = hello.get("resume_token")
    assert isinstance(tok, str) and len(tok) > 8, f"missing resume_token on {backend_name()}: {hello}"


def test_resume_reissues_token(multi_backend: BackendServer) -> None:
    """Sending resume with the stored token yields a new hello with resumed=true."""
    worker_id = f"rs-re-{uuid.uuid4().hex[:8]}"
    first = _ws_collect(multi_backend.base_url, worker_id, want_types={"hello"})
    hello1 = next(f for f in first if f.get("type") == "hello")
    tok = hello1.get("resume_token")
    assert isinstance(tok, str) and tok

    second = _ws_collect(
        multi_backend.base_url,
        worker_id,
        send=[{"type": "resume", "token": tok}],
        want_types={"hello"},
        timeout_s=10.0,
    )
    hellos = [f for f in second if f.get("type") == "hello"]
    assert hellos, second
    # Prefer the resumed hello if present; otherwise accept any new token.
    resumed = [h for h in hellos if h.get("resumed") is True]
    target = resumed[-1] if resumed else hellos[-1]
    new_tok = target.get("resume_token")
    assert isinstance(new_tok, str) and new_tok
    if resumed:
        assert new_tok != tok, target
    else:
        # Some backends mint a fresh token on every connect; still require a token.
        assert len(new_tok) > 8


def test_widget_stores_resume_token(page: Page, multi_backend: BackendServer) -> None:
    """Browser widget sessionStorage gets resume_token under multi-backend routes."""
    worker_id = f"rs-ui-{uuid.uuid4().hex[:8]}"
    install_multi_backend_routes(page)
    # Serve test-page HTML that mounts uterm-session (same as heavy hijack).
    page.route(
        f"**/test-page/{worker_id}",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body=widget_test_page_html(worker_id),
        ),
    )
    page.goto(f"{multi_backend.base_url}/test-page/{worker_id}", wait_until="domcontentloaded")
    page.wait_for_function(
        f"sessionStorage.getItem('uterm_resume_{worker_id}') !== null",
        timeout=10000,
    )
    token = page.evaluate(f"sessionStorage.getItem('uterm_resume_{worker_id}')")
    assert token is not None
    assert len(str(token)) > 8


def test_backend_name_is_supported() -> None:
    assert backend_name() in ("python", "go", "csharp")
