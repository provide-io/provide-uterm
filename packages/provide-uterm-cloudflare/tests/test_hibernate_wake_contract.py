#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Contract proof: Durable Object hibernation wipe → restore → browser fan-out.

CF can evict a DO while WebSockets stay open at the edge. On wake, in-memory
maps (``browser_sockets``, ``worker_ws``, hijack session) are empty; durable
state lives in SQLite + per-socket ``serializeAttachment``. This module is the
**unit-level proof** of that contract (no live CF required).

Live proof (optional): ``pytest -m real_cf packages/provide-uterm-cloudflare/tests/test_e2e_ws.py``.
"""

from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import pytest
from provide.uterm.cloudflare.bridge.hijack import HijackSession
from provide.uterm.cloudflare.contracts import frame_json
from provide.uterm.cloudflare.do.session_runtime import SessionRuntime

from provide.uterm.control_channel import ControlChunk, ControlFrameDecoder, DataChunk

_KEY = "test-secret-key-32-bytes-minimum!"


class _HibernatableWs:
    """WS stub with attachment that survives a simulated DO cold start."""

    def __init__(self, attachment: str) -> None:
        self._attachment = attachment
        self.sent: list[str] = []
        self.closed: tuple[int, str] | None = None

    def serializeAttachment(self, value: object) -> None:  # noqa: N802
        self._attachment = str(value)

    def deserializeAttachment(self) -> object:  # noqa: N802
        return self._attachment

    def send(self, data: str) -> None:
        self.sent.append(data)

    def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)


def _decode(raw: str) -> dict:
    decoder = ControlFrameDecoder()
    events = decoder.feed(raw)
    events.extend(decoder.finish())
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ControlChunk)
    return event.control


def _make_runtime(worker_id: str = "hib-w1") -> SessionRuntime:
    conn = sqlite3.connect(":memory:")
    live_sockets: list[_HibernatableWs] = []
    ctx = SimpleNamespace(
        storage=SimpleNamespace(
            sql=SimpleNamespace(exec=conn.execute),
            setAlarm=lambda ms: None,
        ),
        id=SimpleNamespace(name=lambda: worker_id),
        getWebSockets=lambda: list(live_sockets),
        _live=live_sockets,
    )
    env = SimpleNamespace(
        AUTH_MODE="jwt",
        JWT_ALGORITHMS="HS256",
        JWT_PUBLIC_KEY_PEM=_KEY,
        WORKER_BEARER_TOKEN="test-worker-token-padded-to-32xyz",
        RESUME_TTL_S="120",
        RESUME_ENABLED="1",
    )
    rt = SessionRuntime(ctx, env)
    # Keep a handle for tests to re-register sockets after "eviction".
    rt._test_live = live_sockets  # type: ignore[attr-defined]
    return rt


def _simulate_do_eviction(rt: SessionRuntime) -> None:
    """Wipe every in-memory field CF loses on hibernate (keep store + ctx)."""
    rt.worker_ws = None
    rt._worker_generation = None
    rt.browser_sockets.clear()
    rt.raw_sockets.clear()
    rt.browser_hijack_owner.clear()
    rt.browser_resume_tokens.clear()
    rt.hijack._session = None
    rt.last_snapshot = None
    # Attachment-bearing sockets remain only in ctx.getWebSockets() (edge).


@pytest.mark.asyncio
async def test_hibernate_wake_restores_lease_and_broadcasts_via_get_websockets() -> None:
    """Full hibernate contract in one test:

    1. Persist wall-clock lease + snapshot to SQLite while "warm".
    2. Register a browser socket only via getWebSockets (edge holds it).
    3. Wipe all DO in-memory maps (eviction).
    4. ``_restore_state()`` reloads lease + snapshot from SQLite.
    5. ``broadcast_to_browsers`` fans out via getWebSockets, not empty dicts.
    6. Role comes from attachment (``browser:admin:…``), not in-memory maps.
    """
    rt = _make_runtime("hib-contract")
    browser = _HibernatableWs(f"browser:admin:{rt.worker_id}")
    rt._test_live.append(browser)  # type: ignore[attr-defined]

    # Warm path: persist durable state.
    mono_expiry = time.monotonic() + 600
    session = HijackSession(hijack_id="lease-1", owner="operator", lease_expires_at=mono_expiry)
    rt.persist_lease(session)
    snap = {"type": "snapshot", "screen": "WARM\n", "cols": 80, "rows": 24}
    rt.store.save_snapshot(rt.worker_id, snap)
    rt.last_snapshot = snap

    row = rt.store.load_session(rt.worker_id)
    assert row is not None
    assert row.get("hijack_id") == "lease-1"

    _simulate_do_eviction(rt)
    assert rt.browser_sockets == {}
    assert rt.hijack.session is None
    assert rt.worker_ws is None

    # Wake: restore durable state.
    rt._restore_state()
    assert rt.hijack.session is not None, "lease must survive hibernation via SQLite wall-clock"
    assert rt.hijack.session.owner == "operator"
    assert rt.hijack.session.hijack_id == "lease-1"

    # Identity after wipe must not use ``ws is self.worker_ws``.
    assert rt._socket_role(browser) == "browser"
    assert rt._socket_browser_role(browser) == "admin"

    # Fan-out uses getWebSockets (browser is NOT in browser_sockets).
    browser.sent.clear()
    await rt.broadcast_to_browsers({"type": "worker_status", "status": "online", "ts": time.time()})
    assert browser.sent, "broadcast must reach edge-held socket after hibernation"
    frame = _decode(browser.sent[0])
    assert frame["type"] == "worker_status"


@pytest.mark.asyncio
async def test_hibernate_wake_rebuilds_browser_owner_and_worker_generation() -> None:
    """A live owner and current worker remain authoritative after an isolate wipe."""
    rt = _make_runtime("hib-owner")
    worker = _HibernatableWs("worker:admin:hib-owner")
    assert await rt.register_worker_socket(worker)  # type: ignore[arg-type]
    browser = _HibernatableWs("browser:admin:hib-owner")
    token = "hibernation-owner-token"
    rt.store.create_resume_token(token, rt.worker_id, "admin", 600)
    rt._serialize_socket_attachment(  # type: ignore[arg-type]
        browser,
        role="browser",
        browser_role="admin",
        socket_id="stable-browser-id",
        resume_token=token,
    )
    rt._test_live.extend([worker, browser])  # type: ignore[attr-defined]
    rt._restore_browser_identity(browser)  # type: ignore[arg-type]

    await rt.webSocketMessage(browser, frame_json("hijack_request"))  # type: ignore[arg-type]
    active = rt.hijack.session
    assert active is not None and active.owner == f"browser:{token}"
    assert rt._attachment_data(browser)["hijack_id"] == active.hijack_id  # type: ignore[arg-type]
    warm_incarnation = rt._runtime_incarnation
    warm_activation_seq = rt._runtime_activation_seq
    assert warm_activation_seq >= 1

    # Workerd may reconstruct the object with an unnamed/default DO id. The
    # first socket callback must recover the real worker id from its attachment
    # before loading SQLite state.
    rt.ctx.id = SimpleNamespace(name=lambda: "default")
    cold = SessionRuntime(rt.ctx, rt.env)
    assert cold.worker_id == "default"
    assert cold.hijack.session is None

    await cold.webSocketMessage(worker, frame_json("worker_hello", input_mode="hijack"))  # type: ignore[arg-type]
    worker.sent.clear()
    await cold.webSocketMessage(browser, frame_json("input", data="post-wake-owned-input"))  # type: ignore[arg-type]

    assert cold.worker_id == "hib-owner"
    assert cold._runtime_incarnation != warm_incarnation
    assert cold._runtime_activation_seq == warm_activation_seq + 1
    assert cold.store.load_runtime_activation("hib-owner") == {
        "incarnation": cold._runtime_incarnation,
        "activation_seq": cold._runtime_activation_seq,
    }
    assert cold.hijack.session is not None
    assert cold.worker_ws is worker
    assert cold.browser_hijack_owner["stable-browser-id"] == cold.hijack.session.hijack_id
    decoded = ControlFrameDecoder().feed(worker.sent[-1])
    assert len(decoded) == 1 and isinstance(decoded[0], DataChunk)
    assert decoded[0].data == "post-wake-owned-input"


def test_socket_role_not_identity_after_hibernate() -> None:
    """Post-wake, worker detection must use attachment, not object identity."""
    rt = _make_runtime("hib-role")
    worker = _HibernatableWs(f"worker:admin:{rt.worker_id}")
    # Warm: point worker_ws at this object.
    rt.worker_ws = worker  # type: ignore[assignment]
    assert worker is rt.worker_ws

    _simulate_do_eviction(rt)
    # Same Python object may still exist at the edge, but worker_ws was cleared.
    assert rt.worker_ws is None
    # Wrong pattern: ``ws is self.worker_ws`` → False after hibernate.
    assert worker is not rt.worker_ws
    # Correct pattern:
    assert rt._socket_role(worker) == "worker"


def test_resume_config_from_env() -> None:
    """RESUME_TTL_S / RESUME_ENABLED land on CloudflareConfig."""
    from provide.uterm.cloudflare.config import CloudflareConfig

    env = SimpleNamespace(
        AUTH_MODE="jwt",
        JWT_ALGORITHMS="HS256",
        JWT_PUBLIC_KEY_PEM=_KEY,
        WORKER_BEARER_TOKEN="test-worker-token-padded-to-32xyz",
        HIJACK_LEASE_S="180",
        RESUME_TTL_S="90",
        RESUME_ENABLED="0",
    )
    cfg = CloudflareConfig.from_env(env)
    assert cfg.resume_ttl_s == 90
    assert cfg.resume_enabled is False
    assert cfg.hijack_lease_s == 180


@pytest.mark.asyncio
async def test_hibernate_websocket_open_resume_disabled() -> None:
    """Hibernation restore hello path with RESUME_ENABLED=0.

    Covers lifecycle.py branches when resume is off: no token mint, no
    resume_token field on hello (``if _resume_on`` false arms).
    """
    rt = _make_runtime("hib-resume-off")
    rt.config.resume_enabled = False
    # Socket not pre-registered → already_initialized=False (hibernate path).
    ws = _HibernatableWs(f"browser:viewer:{rt.worker_id}")
    await rt.webSocketOpen(ws)  # type: ignore[arg-type]

    assert ws.sent, "hello must still be sent when resume is disabled"
    hello = _decode(ws.sent[0])
    assert hello["type"] == "hello"
    assert hello.get("resume_supported") is False
    assert "resume_token" not in hello
    assert rt.browser_resume_tokens == {}
