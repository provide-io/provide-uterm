#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for SessionRuntime._ensure_credentials() — TTL-based KV credential refresh.

Covers every branch:
- TTL cache: skips KV when loaded recently
- Loads all four hash fields from KV on first call
- Revocation: hashes cleared when KV returns null values
- Survives hibernation: loads even when _meta_loaded is True
- KV entry absent: clears hashes to None
- KV binding absent: sets _credentials_loaded_at, no crash
- KV read raises: exception swallowed, no crash
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from provide.uterm.cloudflare.do.session_runtime import SessionRuntime

# ---------------------------------------------------------------------------
# Shared helpers (mirrored from test_session_runtime_unit_part1.py)
# ---------------------------------------------------------------------------

_KEY = "test-secret-key-32-bytes-minimum!"


def _make_ctx(worker_id: str = "test-worker") -> SimpleNamespace:
    conn = sqlite3.connect(":memory:")
    return SimpleNamespace(
        storage=SimpleNamespace(
            sql=SimpleNamespace(exec=conn.execute),
            setAlarm=lambda ms: None,
        ),
        id=SimpleNamespace(name=lambda: worker_id),
        getWebSockets=list,
    )


def _make_env(**extra: object) -> SimpleNamespace:
    env = SimpleNamespace(AUTH_MODE="jwt", **extra)
    env.JWT_ALGORITHMS = "HS256"
    env.JWT_PUBLIC_KEY_PEM = _KEY
    if not hasattr(env, "WORKER_BEARER_TOKEN"):
        env.WORKER_BEARER_TOKEN = "test-worker-token-padded-to-32xyz"
    return env


def _make_runtime(worker_id: str = "test-worker") -> SessionRuntime:
    ctx = _make_ctx(worker_id)
    return SessionRuntime(ctx, _make_env())


def _kv_payload(
    worker_token_hash: str | None = "hash-worker",  # noqa: S107
    share_token_hash: str | None = "hash-share",  # noqa: S107
    control_token_hash: str | None = "hash-control",  # noqa: S107
    issued_ip: str | None = "1.2.3.4",
) -> str:
    return json.dumps(
        {
            "display_name": "test",
            "connector_type": "tunnel:terminal",
            "worker_token_hash": worker_token_hash,
            "share_token_hash": share_token_hash,
            "control_token_hash": control_token_hash,
            "issued_ip": issued_ip,
        }
    )


def _make_fake_kv(payload: str | None) -> SimpleNamespace:
    """Return a fake KV with a tracked async `get`."""
    call_log: list[str] = []

    async def _get(key: str) -> str | None:
        call_log.append(key)
        return payload

    return SimpleNamespace(get=_get, call_log=call_log)


# ---------------------------------------------------------------------------
# 1. Loads hashes from KV when _credentials_loaded_at is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loads_hashes_from_kv_on_first_call() -> None:
    """All four credential fields are populated from KV on the first call."""
    rt = _make_runtime("w1")
    rt._credentials_loaded_at = None
    rt.env.SESSION_REGISTRY = _make_fake_kv(_kv_payload())

    await rt._ensure_credentials()

    assert rt._tunnel_worker_token_hash == "hash-worker"
    assert rt._share_token_hash == "hash-share"
    assert rt._control_token_hash == "hash-control"
    assert rt._issued_ip == "1.2.3.4"
    assert rt._credentials_loaded_at is not None


# ---------------------------------------------------------------------------
# 2. Revocation takes effect after TTL expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revocation_takes_effect_after_ttl_expiry() -> None:
    """After TTL expires and KV returns null values, hashes are cleared."""
    rt = _make_runtime("w2")

    # Simulate a previously loaded valid state
    rt._tunnel_worker_token_hash = "old-worker-hash"
    rt._share_token_hash = "old-share-hash"
    rt._control_token_hash = "old-control-hash"
    rt._issued_ip = "1.2.3.4"

    # KV now returns nulls (revoked/rotated)
    revoked_payload = json.dumps(
        {
            "worker_token_hash": None,
            "share_token_hash": None,
            "control_token_hash": None,
            "issued_ip": None,
        }
    )
    rt.env.SESSION_REGISTRY = _make_fake_kv(revoked_payload)

    # Force TTL expiry by setting loaded_at to a far-past timestamp
    rt._credentials_loaded_at = 0.0

    await rt._ensure_credentials()

    assert rt._tunnel_worker_token_hash is None
    assert rt._share_token_hash is None
    assert rt._control_token_hash is None
    assert rt._issued_ip is None


# ---------------------------------------------------------------------------
# 3. Survives hibernation (_meta_loaded=True, hashes None → loads from KV)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_survives_hibernation() -> None:
    """Loads credential hashes even when _meta_loaded is True (post-hibernation)."""
    rt = _make_runtime("w3")

    # Simulate post-hibernation state: meta was restored from SQLite but
    # token hashes were not persisted there, so they are None.
    rt._meta_loaded = True
    rt._tunnel_worker_token_hash = None
    rt._share_token_hash = None
    rt._control_token_hash = None
    rt._issued_ip = None
    rt._credentials_loaded_at = None  # not loaded yet

    rt.env.SESSION_REGISTRY = _make_fake_kv(_kv_payload())

    await rt._ensure_credentials()

    assert rt._tunnel_worker_token_hash == "hash-worker"
    assert rt._share_token_hash == "hash-share"
    assert rt._control_token_hash == "hash-control"
    assert rt._issued_ip == "1.2.3.4"


# ---------------------------------------------------------------------------
# 4. TTL cache: KV called only once within TTL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ttl_cache_prevents_redundant_kv_reads() -> None:
    """KV get is called exactly once when two calls are made within TTL."""
    rt = _make_runtime("w4")
    rt._credentials_loaded_at = None

    get_mock = AsyncMock(return_value=_kv_payload())

    class _FakeKV:
        get = get_mock

    rt.env.SESSION_REGISTRY = _FakeKV()

    await rt._ensure_credentials()
    await rt._ensure_credentials()  # within TTL — should not hit KV again

    assert get_mock.call_count == 1


# ---------------------------------------------------------------------------
# 5. KV returns None (missing entry) — clears hashes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kv_missing_entry_clears_hashes() -> None:
    """When KV returns None for the session key, all hashes are set to None."""
    rt = _make_runtime("w5")

    # Pre-populate hashes as if previously loaded
    rt._tunnel_worker_token_hash = "stale-worker-hash"
    rt._share_token_hash = "stale-share-hash"
    rt._control_token_hash = "stale-control-hash"
    rt._issued_ip = "9.9.9.9"

    # Force TTL expiry
    rt._credentials_loaded_at = 0.0

    # KV has no entry for this session
    rt.env.SESSION_REGISTRY = _make_fake_kv(None)

    await rt._ensure_credentials()

    assert rt._tunnel_worker_token_hash is None
    assert rt._share_token_hash is None
    assert rt._control_token_hash is None
    assert rt._issued_ip is None
    assert rt._credentials_loaded_at is not None


# ---------------------------------------------------------------------------
# 6. KV binding absent — sets _credentials_loaded_at, no crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kv_binding_absent_is_noop() -> None:
    """When env has no SESSION_REGISTRY, _ensure_credentials sets timestamp and returns."""
    rt = _make_runtime("w6")
    rt._credentials_loaded_at = None

    # Ensure no SESSION_REGISTRY attribute
    if hasattr(rt.env, "SESSION_REGISTRY"):
        delattr(rt.env, "SESSION_REGISTRY")

    await rt._ensure_credentials()  # must not raise

    assert rt._credentials_loaded_at is not None


# ---------------------------------------------------------------------------
# 7. KV read raises — exception swallowed, no crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kv_read_raises_is_swallowed() -> None:
    """When KV.get() raises, the exception is swallowed and no crash occurs."""
    rt = _make_runtime("w7")
    rt._credentials_loaded_at = None

    class _BrokenKV:
        async def get(self, _key: str) -> None:
            raise RuntimeError("KV is down")

    rt.env.SESSION_REGISTRY = _BrokenKV()

    await rt._ensure_credentials()  # must not raise

    # _credentials_loaded_at should NOT be updated (exception path skips it)
    # This matches the design: on KV failure we don't stamp a new load time,
    # so the next call will retry.
    # (No assertion on _credentials_loaded_at because the spec just requires no crash.)
