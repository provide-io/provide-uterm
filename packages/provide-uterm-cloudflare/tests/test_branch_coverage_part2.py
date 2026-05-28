#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Branch coverage tests — covers branches not hit by any existing test suite.

Each section corresponds to a file/line range reported by --cov-branch.
"""

from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers shared across sections
# ---------------------------------------------------------------------------


def _make_ctx(worker_id: str = "test-worker") -> SimpleNamespace:
    conn = sqlite3.connect(":memory:")
    return SimpleNamespace(
        storage=SimpleNamespace(
            sql=SimpleNamespace(exec=conn.execute),
            setAlarm=lambda ms: None,
        ),
        id=SimpleNamespace(name=lambda: worker_id),
        getWebSockets=list,
        acceptWebSocket=lambda ws: None,
    )


def _make_env(mode: str = "jwt") -> SimpleNamespace:
    # from_env only accepts jwt mode now; always emit a valid jwt config.
    return SimpleNamespace(
        AUTH_MODE="jwt",
        JWT_ALGORITHMS="HS256",
        JWT_PUBLIC_KEY_PEM="test-secret-key-32-bytes-minimum!",
        WORKER_BEARER_TOKEN="test-worker-token",
    )


def _make_runtime(worker_id: str = "test-worker", mode: str = "dev"):
    from provide.uterm.cloudflare.do.session_runtime import SessionRuntime

    # Override the in-memory mode for tests exercising the legacy open-access branches.
    rt = SessionRuntime(_make_ctx(worker_id), _make_env())
    rt.config.jwt.mode = mode
    return rt


# ---------------------------------------------------------------------------
# api/http_routes.py  76->81  — _wait_for_prompt: snapshot is None during poll
# ---------------------------------------------------------------------------


async def test_wait_for_prompt_snapshot_none_during_poll() -> None:
    """Cover the `if snapshot:` false branch — snapshot stays None inside the loop."""
    from provide.uterm.cloudflare.api.http_routes._shared import _wait_for_prompt

    class _NoSnapshotRuntime:
        last_snapshot = None  # always None

    result = await _wait_for_prompt(
        _NoSnapshotRuntime(),
        expect_prompt_id="some_prompt",
        expect_regex=None,
        timeout_ms=100,
        poll_interval_ms=50,
    )
    # Returns None (last_snapshot) after timeout
    assert result is None


# ---------------------------------------------------------------------------
# api/http_routes.py  144->146  — acquire: renewal path skips pause
# ---------------------------------------------------------------------------


async def test_acquire_renewal_skips_pause() -> None:
    """Cover the `if not result.is_renewal:` false branch (renewal → no pause sent)."""
    from provide.uterm.cloudflare.api.http_routes import route_http
    from provide.uterm.cloudflare.bridge.hijack import HijackCoordinator

    class _Req:
        def __init__(self, url: str, *, method: str = "GET", body: dict | None = None):
            self.url = url
            self.method = method
            self._body = json.dumps(body or {})

        async def text(self) -> str:
            return self._body

    pause_calls: list[str] = []

    class _RT:
        worker_id = "w"
        worker_ws = object()
        hijack = HijackCoordinator()
        last_snapshot = None
        last_analysis = None
        lifecycle_state = "stopped"
        input_mode = "hijack"
        browser_hijack_owner: dict = {}
        browser_resume_tokens: dict = {}

        async def browser_role_for_request(self, req):
            return "admin"

        async def request_json(self, req):
            return json.loads(req._body)

        def persist_lease(self, session):
            pass

        def clear_lease(self):
            pass

        async def push_worker_control(self, action, *, owner, lease_s):
            pause_calls.append(action)
            return True

        async def broadcast_hijack_state(self):
            pass

        async def push_worker_input(self, data):
            return True

        async def send_ws(self, ws, frame):
            pass

        def ws_key(self, ws):
            return str(id(ws))

        def _socket_browser_role(self, ws):
            return "admin"

        @property
        def store(self):
            return SimpleNamespace(
                list_events_since=lambda *a, **k: [],
                load_session=lambda *a, **k: None,
                current_event_seq=lambda *a, **k: 0,
                min_event_seq=lambda *a, **k: 0,
                save_input_mode=lambda *a, **k: None,
            )

    rt = _RT()

    # First acquire — fresh (is_renewal=False) → pause sent
    r1 = await route_http(rt, _Req("https://x/w/hijack/acquire", method="POST", body={"owner": "alice", "lease_s": 60}))
    assert r1.status == 200
    assert "pause" in pause_calls
    pause_calls.clear()

    # Second acquire by same owner — renewal (is_renewal=True) → pause NOT sent
    r2 = await route_http(rt, _Req("https://x/w/hijack/acquire", method="POST", body={"owner": "alice", "lease_s": 60}))
    assert r2.status == 200
    assert "pause" not in pause_calls


# ---------------------------------------------------------------------------
# auth/jwt.py  45->48  — _fetch_jwks: stale cache entry is refreshed
# ---------------------------------------------------------------------------


async def test_fetch_jwks_stale_cache_triggers_refetch() -> None:
    """Line 45->48: cache entry exists but is past TTL → network call is made again."""
    from provide.uterm.cloudflare.auth import jwt as jwt_module
    from provide.uterm.cloudflare.auth.jwt import _JWKS_CACHE_TTL_S, _fetch_jwks

    url = "https://example.com/.well-known/jwks-stale.json"
    stale_data: dict = {"keys": [{"kty": "old"}]}
    fresh_data: dict = {"keys": [{"kty": "new"}]}

    # Plant a stale cache entry (timestamp well past TTL)
    jwt_module._JWKS_CACHE[url] = (time.monotonic() - _JWKS_CACHE_TTL_S - 10, stale_data)

    encoded = json.dumps(fresh_data).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = encoded
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = await _fetch_jwks(url)

    # Fresh data returned after re-fetch
    assert result == fresh_data
    # Cache updated with fresh data
    assert jwt_module._JWKS_CACHE[url][1] == fresh_data


# ---------------------------------------------------------------------------
# auth/jwt.py  89->82  — _resolve_signing_key: no-kid alg-mismatch continues loop
# ---------------------------------------------------------------------------


async def test_resolve_signing_key_no_kid_alg_mismatch_skips_key() -> None:
    """Line 89->82: no-kid path where key_alg != alg → loop continues, no match → raises."""
    import jwt as pyjwt
    from provide.uterm.cloudflare.auth.jwt import JwtValidationError, _resolve_signing_key
    from provide.uterm.cloudflare.config import JwtConfig

    config = JwtConfig(
        mode="jwt",
        jwks_url="https://example.com/.well-known/jwks-mismatch.json",
        algorithms=("RS256",),
    )

    # Token without kid header — triggers no-kid algorithm-matching path
    token = pyjwt.encode(
        {"sub": "u", "exp": int(time.time()) + 600},
        "uterm-test-secret-32-byte-minimum-key",
        algorithm="HS256",
        # Deliberately no kid
    )

    # A key whose algorithm_name doesn't match the token's "alg" header
    mismatch_key = MagicMock()
    mismatch_key.key_id = None  # no kid → enters no-kid branch
    mismatch_key.algorithm_name = "ES256"  # mismatches "HS256" in header
    mismatch_key.key = object()

    with (
        patch("provide.uterm.cloudflare.auth.jwt._fetch_jwks", new=AsyncMock(return_value={})),
        patch("jwt.PyJWKSet.from_dict", return_value=MagicMock(keys=[mismatch_key])),
        patch("jwt.get_unverified_header", return_value={"alg": "HS256"}),
        pytest.raises(JwtValidationError, match="no matching key"),
    ):
        await _resolve_signing_key(token, config)


# ---------------------------------------------------------------------------
# cli.py  21->exit  — _require_pywrangler: pywrangler found (no raise)
# ---------------------------------------------------------------------------


def test_persist_lease_no_set_alarm_does_not_raise() -> None:
    """Line 377->exit: persist_lease with storage that has no setAlarm → skips alarm."""
    import sqlite3

    from provide.uterm.cloudflare.bridge.hijack import HijackSession
    from provide.uterm.cloudflare.do.session_runtime import SessionRuntime

    conn = sqlite3.connect(":memory:")
    ctx = SimpleNamespace(
        storage=SimpleNamespace(
            sql=SimpleNamespace(exec=conn.execute),
            # No setAlarm attribute → callable(getattr(..., "setAlarm", None)) is False
        ),
        id=SimpleNamespace(name=lambda: "w1"),
        getWebSockets=list,
        acceptWebSocket=lambda ws: None,
    )
    rt = SessionRuntime(ctx, _make_env("dev"))
    session = HijackSession(
        hijack_id="test-hid",
        owner="alice",
        lease_expires_at=time.time() + 60,
    )
    # Should not raise even though setAlarm is absent
    rt.persist_lease(session)


# ---------------------------------------------------------------------------
# do/session_runtime.py  491->exit  — alarm: worker_ws present, no setAlarm available
# ---------------------------------------------------------------------------


async def test_alarm_worker_connected_no_set_alarm() -> None:
    """Line 491->exit: alarm() with worker_ws set but ctx.storage has no setAlarm."""
    import sqlite3

    from provide.uterm.cloudflare.do.session_runtime import SessionRuntime

    conn = sqlite3.connect(":memory:")
    ctx = SimpleNamespace(
        storage=SimpleNamespace(
            sql=SimpleNamespace(exec=conn.execute),
            # No setAlarm attribute → getattr returns None
        ),
        id=SimpleNamespace(name=lambda: "w1"),
        getWebSockets=list,
        acceptWebSocket=lambda ws: None,
    )
    rt = SessionRuntime(ctx, _make_env("dev"))

    # Simulate an active worker connection
    ws_stub = SimpleNamespace(send=lambda d: None, deserializeAttachment=lambda: "worker:admin:w1")
    rt.worker_ws = ws_stub

    mock_kv = AsyncMock()
    with patch("provide.uterm.cloudflare.do.session_runtime.io.update_kv_session", mock_kv):
        await rt.alarm()

    mock_kv.assert_awaited_once()


# ---------------------------------------------------------------------------
# do/session_runtime.py  494->exit  — alarm: no worker, hijack active, no setAlarm
# ---------------------------------------------------------------------------


async def test_alarm_no_worker_active_hijack_no_set_alarm() -> None:
    """Line 494->exit: alarm() with worker_ws=None, hijack.session present, no setAlarm."""
    import sqlite3

    from provide.uterm.cloudflare.bridge.hijack import HijackSession
    from provide.uterm.cloudflare.do.session_runtime import SessionRuntime

    conn = sqlite3.connect(":memory:")
    ctx = SimpleNamespace(
        storage=SimpleNamespace(
            sql=SimpleNamespace(exec=conn.execute),
            # No setAlarm attribute → inner if is False → 494->exit
        ),
        id=SimpleNamespace(name=lambda: "w1"),
        getWebSockets=list,
        acceptWebSocket=lambda ws: None,
    )
    rt = SessionRuntime(ctx, _make_env("dev"))
    rt.worker_ws = None

    # Plant an active (non-expired) hijack session so the elif branch is True
    rt.hijack._session = HijackSession(
        hijack_id="hid",
        owner="alice",
        lease_expires_at=time.time() + 3600,
    )

    # Should not raise — setAlarm just won't be called
    await rt.alarm()


# ---------------------------------------------------------------------------
# _mono_to_wall: None branch (both modules have identical helpers)
# ---------------------------------------------------------------------------


def test_mono_to_wall_none_shared() -> None:
    """_shared._mono_to_wall(None) → None  (line 52)."""
    from provide.uterm.cloudflare.api.http_routes._shared import _mono_to_wall

    assert _mono_to_wall(None) is None


def test_mono_to_wall_value_shared() -> None:
    """_shared._mono_to_wall(float) → wall-clock float."""
    from provide.uterm.cloudflare.api.http_routes._shared import _mono_to_wall

    result = _mono_to_wall(1000.0)
    assert isinstance(result, float)


def test_mono_to_wall_none_io() -> None:
    """session_runtime.io._mono_to_wall(None) → None  (line 47)."""
    from provide.uterm.cloudflare.do.session_runtime.io import _mono_to_wall

    assert _mono_to_wall(None) is None


def test_mono_to_wall_value_io() -> None:
    """session_runtime.io._mono_to_wall(float) → wall-clock float."""
    from provide.uterm.cloudflare.do.session_runtime.io import _mono_to_wall

    result = _mono_to_wall(1000.0)
    assert isinstance(result, float)
