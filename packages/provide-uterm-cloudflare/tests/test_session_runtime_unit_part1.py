#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Unit tests for do/session_runtime.py — all non-CF-runtime branches."""

from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import jwt
import pytest
from provide.uterm.cloudflare.do.session_runtime import SessionRuntime
from provide.uterm.cloudflare.state.store import LeaseRecord

from provide.uterm.control_channel import ControlChannelDecoder, ControlChunk, DataChunk

_KEY = "test-secret-key-32-bytes-minimum!"


def _make_token(sub: str = "user", roles: list[str] | None = None) -> str:
    now = int(time.time())
    payload: dict = {"sub": sub, "iat": now, "exp": now + 600}
    if roles:
        payload["roles"] = roles
    return jwt.encode(payload, _KEY, algorithm="HS256")


def _make_ctx(worker_id: str = "test-worker"):
    conn = sqlite3.connect(":memory:")
    return SimpleNamespace(
        storage=SimpleNamespace(
            sql=SimpleNamespace(exec=conn.execute),
            setAlarm=lambda ms: None,
        ),
        id=SimpleNamespace(name=lambda: worker_id),
        getWebSockets=list,
    )


def _make_env(mode: str = "jwt", **extra) -> SimpleNamespace:
    # from_env only accepts jwt mode now; always emit a valid jwt config.
    env = SimpleNamespace(AUTH_MODE="jwt", **extra)
    env.JWT_ALGORITHMS = "HS256"
    env.JWT_PUBLIC_KEY_PEM = _KEY
    if not hasattr(env, "WORKER_BEARER_TOKEN"):
        env.WORKER_BEARER_TOKEN = "test-worker-token-padded-to-32xyz"
    return env


def _make_runtime(worker_id: str = "test-worker", mode: str = "dev") -> SessionRuntime:
    # from_env only accepts jwt mode now; build a valid jwt config, then override
    # the in-memory mode for tests that exercise the legacy open-access branches
    # (defense-in-depth code reachable only via direct config construction).
    ctx = _make_ctx(worker_id)
    rt = SessionRuntime(ctx, _make_env("jwt"))
    rt.config.jwt.mode = mode
    return rt


def _decode_sent(raw: str, *, data_frame_type: str | None = None) -> dict:
    decoder = ControlChannelDecoder()
    events = decoder.feed(raw)
    events.extend(decoder.finish())
    assert len(events) == 1
    event = events[0]
    if isinstance(event, ControlChunk):
        return event.control
    if isinstance(event, DataChunk):
        return {"type": data_frame_type or "term", "data": event.data}
    raise AssertionError("unexpected decoder event")


class _MockWs:
    """Sync-send WebSocket stub."""

    def __init__(self, attachment: object = None) -> None:
        self._attachment = attachment
        self.sent: list[str] = []

    def deserializeAttachment(self) -> object:  # noqa: N802
        return self._attachment

    def send(self, data: str) -> None:
        self.sent.append(data)


class _AsyncWs(_MockWs):
    """Async-send WebSocket stub."""

    async def send(self, data: str) -> None:  # type: ignore[override]
        self.sent.append(data)


class _MockRequest:
    """Minimal HTTP request stub."""

    def __init__(
        self,
        url: str = "https://x/worker/test-worker/api/health",
        method: str = "GET",
        headers: dict | None = None,
        body: str = "{}",
    ) -> None:
        self.url = url
        self.method = method
        self._headers = headers or {}
        self._body = body
        self.headers = SimpleNamespace(get=lambda k, d=None: self._headers.get(k, d))

    async def text(self) -> str:
        return self._body


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


def test_constructor_missing_sql_raises() -> None:
    """Line 44: ctx without storage.sql.exec → RuntimeError."""
    ctx = SimpleNamespace(storage=SimpleNamespace(), id=SimpleNamespace(name=lambda: "w"))
    with pytest.raises(RuntimeError, match="sqlite storage"):
        SessionRuntime(ctx, _make_env())


def test_constructor_derives_worker_id() -> None:
    """Lines 59-63: _derive_worker_id uses ctx.id.name()."""
    rt = _make_runtime("my-worker")
    assert rt.worker_id == "my-worker"


def test_derive_worker_id_non_callable_name() -> None:
    """Lines 66-70: ctx.id.name is not callable → 'default'."""
    ctx = _make_ctx("any")
    ctx.id = SimpleNamespace(name="not-callable")
    rt = SessionRuntime(ctx, _make_env())
    assert rt.worker_id == "default"


def test_derive_worker_id_name_raises() -> None:
    """Lines 64-70: ctx.id.name() raises → 'default'."""

    def bad_name() -> str:
        raise RuntimeError("failed")

    ctx = _make_ctx("any")
    ctx.id = SimpleNamespace(name=bad_name)
    rt = SessionRuntime(ctx, _make_env())
    assert rt.worker_id == "default"


# ---------------------------------------------------------------------------
# ws_key
# ---------------------------------------------------------------------------


def test_ws_key_generates_unique_keys() -> None:
    """Lines 73-83: two different ws objects get different keys."""
    rt = _make_runtime()
    ws1, ws2 = _MockWs(), _MockWs()
    assert rt.ws_key(ws1) != rt.ws_key(ws2)


def test_ws_key_cached() -> None:
    """Lines 74-77: key is cached on ws object."""
    rt = _make_runtime()
    ws = _MockWs()
    assert rt.ws_key(ws) == rt.ws_key(ws)


# ---------------------------------------------------------------------------
# _restore_state
# ---------------------------------------------------------------------------


def test_restore_state_with_saved_lease() -> None:
    """Lines 89-102: restore a live hijack session from SQLite."""
    rt = _make_runtime("w1")
    rt.store.save_lease(
        LeaseRecord(worker_id="w1", hijack_id="hid-123", owner="alice", lease_expires_at=time.time() + 300)
    )
    rt.hijack._session = None
    rt._restore_state()
    assert rt.hijack.session is not None
    assert rt.hijack.session.owner == "alice"


def test_restore_state_with_snapshot() -> None:
    """Lines 103-105: restore last_snapshot from SQLite."""
    rt = _make_runtime()
    rt.store.save_snapshot(rt.worker_id, {"type": "snapshot", "screen": "hello"})
    rt.last_snapshot = None
    rt._restore_state()
    assert rt.last_snapshot is not None


def test_restore_state_with_input_mode() -> None:
    """Lines 106-108: restore input_mode from SQLite."""
    rt = _make_runtime()
    rt.store.save_input_mode(rt.worker_id, "open")
    rt.input_mode = "hijack"
    rt._restore_state()
    assert rt.input_mode == "open"


def test_restore_state_with_deleted_tombstone() -> None:
    """A deleted session restores as deleted instead of reviving state."""
    rt = _make_runtime("w1")
    rt.store.mark_deleted("w1")
    rt.lifecycle_state = "running"
    rt._deleted_at = None
    rt._restore_state()
    assert rt.lifecycle_state == "deleted"
    assert rt._deleted_at is not None


def test_restore_state_loads_meta_from_sqlite() -> None:
    """_restore_state loads session metadata from SQLite when present."""
    rt = _make_runtime("w1")
    rt.store.save_session_meta(
        "w1",
        {
            "display_name": "Saved Name",
            "connector_type": "ssh",
            "created_at": 1000.0,
            "tags": ["a"],
            "visibility": "private",
            "owner": "alice",
        },
    )
    rt.meta = {"display_name": "w1"}  # reset
    rt._meta_loaded = False
    rt._restore_state()
    assert rt.meta["display_name"] == "Saved Name"
    assert rt.meta["connector_type"] == "ssh"
    assert rt._meta_loaded is True


@pytest.mark.asyncio
async def test_ensure_meta_loads_from_kv() -> None:
    """_ensure_meta reads KV on first contact when SQLite has no meta."""
    import json as _json

    rt = _make_runtime("w1")
    kv_data = _json.dumps(
        {
            "display_name": "KV Session",
            "connector_type": "telnet",
            "created_at": 2000.0,
            "tags": ["prod"],
            "visibility": "public",
            "owner": "bob",
        }
    )

    class _FakeKV:
        async def get(self, key):
            return kv_data if key == "session:w1" else None

    rt.env.SESSION_REGISTRY = _FakeKV()
    rt._meta_loaded = False
    await rt._ensure_meta()
    assert rt.meta["display_name"] == "KV Session"
    assert rt.meta["connector_type"] == "telnet"
    assert rt._meta_loaded is True
    # Should be persisted to SQLite
    saved = rt.store.load_session_meta("w1")
    assert saved is not None
    assert saved["display_name"] == "KV Session"


@pytest.mark.asyncio
async def test_ensure_meta_no_kv_binding() -> None:
    """_ensure_meta with no SESSION_REGISTRY is a no-op."""
    rt = _make_runtime("w1")
    rt._meta_loaded = False
    await rt._ensure_meta()
    assert rt._meta_loaded is True
    assert rt.meta["display_name"] == "w1"  # default


@pytest.mark.asyncio
async def test_ensure_meta_kv_error_is_swallowed() -> None:
    """_ensure_meta swallows KV read errors gracefully."""

    class _BrokenKV:
        async def get(self, _key):
            raise RuntimeError("KV down")

    rt = _make_runtime("w1")
    rt.env.SESSION_REGISTRY = _BrokenKV()
    rt._meta_loaded = False
    await rt._ensure_meta()  # should not raise
    assert rt._meta_loaded is True


@pytest.mark.asyncio
async def test_ensure_meta_idempotent() -> None:
    """_ensure_meta only runs once (second call is a no-op)."""
    rt = _make_runtime("w1")
    rt._meta_loaded = False

    class _FakeKV:
        call_count = 0

        async def get(self, _key):
            _FakeKV.call_count += 1
            return

    rt.env.SESSION_REGISTRY = _FakeKV()
    await rt._ensure_meta()
    await rt._ensure_meta()  # second call should skip
    assert _FakeKV.call_count == 1


# ---------------------------------------------------------------------------
# _extract_token
# ---------------------------------------------------------------------------


def test_extract_token_from_bearer_header() -> None:
    """Lines 118-123: Authorization: Bearer xyz → 'xyz'."""
    rt = _make_runtime()
    req = _MockRequest(headers={"Authorization": "Bearer my-token"})
    assert rt._extract_token(req) == "my-token"


def test_extract_token_query_param_ignored() -> None:
    """Bearer query params are not an auth transport."""
    rt = _make_runtime()
    req = _MockRequest(url="https://x/path?token=qtoken")
    assert rt._extract_token(req) is None


def test_extract_token_access_token_query_param_ignored() -> None:
    """OAuth-style access_token query params are not an auth transport."""
    rt = _make_runtime()
    req = _MockRequest(url="https://x/path?access_token=qtoken")
    assert rt._extract_token(req) is None
