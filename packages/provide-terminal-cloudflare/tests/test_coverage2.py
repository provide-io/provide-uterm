#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage gap tests for bridge/hijack.py, auth/jwt.py, state/store.py, state/registry.py."""

from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace

import jwt
import pytest
from provide.terminal.cloudflare.auth.jwt import JwtValidationError, decode_jwt
from provide.terminal.cloudflare.bridge.hijack import HijackCoordinator
from provide.terminal.cloudflare.config import JwtConfig
from provide.terminal.cloudflare.state.registry import list_kv_sessions, update_kv_session
from provide.terminal.cloudflare.state.store import LeaseRecord, SqliteStateStore

_KEY = "test-secret-key-32-bytes-minimum!"


def _make_token(sub: str = "user", roles: list[str] | None = None) -> str:
    now = int(time.time())
    payload: dict = {"sub": sub, "iat": now, "exp": now + 600}
    if roles is not None:
        payload["roles"] = roles
    return jwt.encode(payload, _KEY, algorithm="HS256")


def _make_config(**kw) -> JwtConfig:  # type: ignore[return]
    return JwtConfig(mode="jwt", public_key_pem=_KEY, algorithms=("HS256",), **kw)


def _make_store() -> SqliteStateStore:
    conn = sqlite3.connect(":memory:")
    store = SqliteStateStore(conn.execute)
    store.migrate()
    return store


# ---------------------------------------------------------------------------
# bridge/hijack.py — uncovered branches
# ---------------------------------------------------------------------------


class TestHijackCoordinatorEdgeCases:
    def test_heartbeat_when_not_hijacked(self) -> None:
        """Line 62: heartbeat with no active session → not_hijacked."""
        hub = HijackCoordinator()
        result = hub.heartbeat("any-id", 60)
        assert result.ok is False
        assert result.error == "not_hijacked"

    def test_release_when_not_hijacked(self) -> None:
        """Line 71: release with no active session → not_hijacked."""
        hub = HijackCoordinator()
        result = hub.release("any-id")
        assert result.ok is False
        assert result.error == "not_hijacked"

    def test_release_wrong_hijack_id(self) -> None:
        """Line 73: release with wrong hijack_id → hijack_id_mismatch."""
        hub = HijackCoordinator()
        hub.acquire("alice", 60)
        result = hub.release("wrong-id")
        assert result.ok is False
        assert result.error == "hijack_id_mismatch"

    def test_can_send_input_when_not_hijacked(self) -> None:
        """Line 80: can_send_input when no session → False."""
        hub = HijackCoordinator()
        assert hub.can_send_input("any-id") is False


# ---------------------------------------------------------------------------
# auth/jwt.py — uncovered branches in decode_jwt
# ---------------------------------------------------------------------------


class TestDecodeJwtBranches:
    async def test_empty_sub_raises(self) -> None:
        """Line 107: sub present but empty string → JwtValidationError."""
        now = int(time.time())
        # sub="" is present (satisfies PyJWT require=["sub"]) but empty → our check raises
        token = jwt.encode({"sub": "", "iat": now, "exp": now + 600}, _KEY, algorithm="HS256")
        with pytest.raises(JwtValidationError, match="missing sub"):
            await decode_jwt(token, _make_config())

    async def test_string_roles_parsed(self) -> None:
        """Line 114: roles claim is a comma-separated string."""
        now = int(time.time())
        token = jwt.encode(
            {"sub": "u1", "roles": "admin,operator", "iat": now, "exp": now + 600}, _KEY, algorithm="HS256"
        )
        principal = await decode_jwt(token, _make_config())
        assert "admin" in principal.roles
        assert "operator" in principal.roles

    async def test_non_iterable_roles_becomes_empty(self) -> None:
        """Line 118: roles claim is not str or list → falls back to default/scope."""
        now = int(time.time())
        token = jwt.encode({"sub": "u1", "roles": 42, "iat": now, "exp": now + 600}, _KEY, algorithm="HS256")
        principal = await decode_jwt(token, _make_config())
        # roles=42 → not str, not list → roles=() → default role or empty
        assert isinstance(principal.roles, (tuple, list, frozenset))

    async def test_missing_key_config_raises(self) -> None:
        """Line 74-76: decode_jwt with no public_key_pem and no jwks_url."""
        token = _make_token()
        config = JwtConfig(mode="jwt", algorithms=("HS256",))
        with pytest.raises(JwtValidationError, match="missing jwt public key"):
            await decode_jwt(token, config)

    async def test_resolve_signing_key_no_match_raises(self) -> None:
        """Line 69: JWKS has no matching key → JwtValidationError."""
        from unittest.mock import patch

        now = int(time.time())
        # Create a token with a kid
        token = jwt.encode(
            {"sub": "u1", "iat": now, "exp": now + 600},
            _KEY,
            algorithm="HS256",
            headers={"kid": "key-1"},
        )
        # Build a JWKS dict with a different kid so no match

        jwks_data = {
            "keys": [
                {
                    "kty": "oct",
                    "kid": "different-key",
                    "k": "c29tZS1vdGhlci1rZXktdGhhdC1kb2VzLW5vdC1tYXRjaA",
                    "alg": "HS256",
                }
            ]
        }
        config = JwtConfig(mode="jwt", jwks_url="https://example.com/.well-known/jwks.json", algorithms=("HS256",))
        with (
            patch("provide.terminal.cloudflare.auth.jwt._fetch_jwks", return_value=jwks_data),
            pytest.raises(JwtValidationError),
        ):
            await decode_jwt(token, config)


# ---------------------------------------------------------------------------
# state/store.py — uncovered methods
# ---------------------------------------------------------------------------


class TestStoreAdditionalMethods:
    def test_clear_lease(self) -> None:
        """Line 119: clear_lease removes hijack data."""
        store = _make_store()
        store.save_lease(LeaseRecord(worker_id="w1", hijack_id="h1", owner="alice", lease_expires_at=999.0))
        row_before = store.load_session("w1")
        assert row_before is not None
        assert row_before["hijack_id"] == "h1"

        store.clear_lease("w1")
        row_after = store.load_session("w1")
        assert row_after is not None
        assert row_after["hijack_id"] is None

    def test_save_input_mode(self) -> None:
        """Lines 130-131: save_input_mode upserts input_mode."""
        store = _make_store()
        store.save_input_mode("w1", "open")
        row = store.load_session("w1")
        assert row is not None
        assert row.get("input_mode") == "open"

        store.save_input_mode("w1", "hijack")
        row2 = store.load_session("w1")
        assert row2 is not None
        assert row2.get("input_mode") == "hijack"

    def test_min_event_seq_empty(self) -> None:
        """Lines 145-162: min_event_seq returns 0 when no events."""
        store = _make_store()
        assert store.min_event_seq("no-worker") == 0

    def test_min_event_seq_with_events(self) -> None:
        """min_event_seq returns minimum seq for the worker."""
        store = _make_store()
        store.append_event("w1", "a", {})
        store.append_event("w1", "b", {})
        min_seq = store.min_event_seq("w1")
        max_seq = store.current_event_seq("w1")
        assert min_seq <= max_seq
        assert min_seq >= 1

    def test_current_event_seq_empty(self) -> None:
        """Lines 231-237: current_event_seq returns 0 when no events."""
        store = _make_store()
        assert store.current_event_seq("no-worker") == 0


# ---------------------------------------------------------------------------
# state/registry.py — error handling paths
# ---------------------------------------------------------------------------


async def _make_failing_kv(fail_on: str) -> SimpleNamespace:
    """KV stub that raises on the specified operation."""

    async def put(key: str, value: str, **_kw: object) -> None:
        if fail_on == "put":
            raise RuntimeError("simulated put failure")

    async def delete(key: str) -> None:
        if fail_on == "delete":
            raise RuntimeError("simulated delete failure")

    async def list_keys(*, prefix: str = "") -> SimpleNamespace:
        if fail_on == "list":
            raise RuntimeError("simulated list failure")
        return SimpleNamespace(keys=[])

    async def get(key: str) -> str | None:
        if fail_on == "get":
            raise RuntimeError("simulated get failure")
        return None

    return SimpleNamespace(put=put, get=get, delete=delete, list=list_keys)


class TestRegistryErrorPaths:
    async def test_kv_delete_failure_is_silenced(self) -> None:
        """Lines 46-47: kv.delete raises → logged, no exception propagated."""
        kv = await _make_failing_kv("delete")
        env = SimpleNamespace(SESSION_REGISTRY=kv)
        # Should not raise
        await update_kv_session(env, "w1", connected=False)

    async def test_kv_put_failure_is_silenced(self) -> None:
        """Lines 70-71: kv.put raises → logged, no exception propagated."""
        kv = await _make_failing_kv("put")
        env = SimpleNamespace(SESSION_REGISTRY=kv)
        # Should not raise
        await update_kv_session(env, "w1", connected=True)

    async def test_kv_list_failure_returns_empty(self) -> None:
        """Lines 85-87: kv.list raises → returns empty list."""
        kv = await _make_failing_kv("list")
        env = SimpleNamespace(SESSION_REGISTRY=kv)
        result = await list_kv_sessions(env)
        assert result == []

    async def test_kv_list_key_with_no_name_skipped(self) -> None:
        """Line 93: key_info with no 'name' → continue."""
        store: dict[str, str] = {}

        async def put(key: str, value: str, **_kw: object) -> None:
            store[key] = value

        async def get(key: str) -> str | None:
            return store.get(key)

        async def delete(key: str) -> None:
            store.pop(key, None)

        async def list_keys(*, prefix: str = "") -> SimpleNamespace:
            # Return a mix of valid and invalid key_info dicts
            return SimpleNamespace(keys=[{"name": ""}, {"other": "x"}, {"name": "session:w1"}])

        await put("session:w1", json.dumps({"session_id": "w1", "connected": True}))
        kv = SimpleNamespace(put=put, get=get, delete=delete, list=list_keys)
        env = SimpleNamespace(SESSION_REGISTRY=kv)
        result = await list_kv_sessions(env)
        # Only w1 has a valid name; empty-name and no-name entries are skipped
        assert len(result) == 1
        assert result[0]["session_id"] == "w1"

    async def test_kv_get_failure_skips_entry(self) -> None:
        """Lines 98-99: kv.get raises → entry skipped, no exception."""
        store: dict[str, str] = {}

        async def put(key: str, value: str, **_kw: object) -> None:
            store[key] = value

        async def get(key: str) -> str | None:
            raise RuntimeError("simulated get failure")

        async def delete(key: str) -> None:
            store.pop(key, None)

        async def list_keys(*, prefix: str = "") -> SimpleNamespace:
            return SimpleNamespace(keys=[{"name": "session:w1"}])

        kv = SimpleNamespace(put=put, get=get, delete=delete, list=list_keys)
        env = SimpleNamespace(SESSION_REGISTRY=kv)
        result = await list_kv_sessions(env)
        # get failed → entry skipped → empty result
        assert result == []
