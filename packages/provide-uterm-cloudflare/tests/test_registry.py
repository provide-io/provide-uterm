"""Tests for the KV session registry (state/registry.py)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from provide.uterm.cloudflare.state.registry import get_kv_session, list_kv_sessions, update_kv_session


def _make_kv() -> SimpleNamespace:
    """In-memory KV stub: put/get/delete/list backed by a plain dict."""
    store: dict[str, str] = {}

    async def put(key: str, value: str, **_kwargs: object) -> None:
        store[key] = value

    async def get(key: str) -> str | None:
        return store.get(key)

    async def delete(key: str) -> None:
        store.pop(key, None)

    async def list_keys(*, prefix: str = "") -> SimpleNamespace:
        keys = [{"name": k} for k in store if k.startswith(prefix)]
        return SimpleNamespace(keys=keys)

    kv = SimpleNamespace(put=put, get=get, delete=delete, list=list_keys)
    kv._store = store
    return kv


@pytest.mark.asyncio
async def test_update_kv_session_noop_without_binding() -> None:
    env = SimpleNamespace()  # no SESSION_REGISTRY attribute
    # Must not raise.
    await update_kv_session(env, "w1", connected=True)
    await update_kv_session(env, "w1", connected=False)


@pytest.mark.asyncio
async def test_update_kv_session_writes_on_connect() -> None:
    kv = _make_kv()
    env = SimpleNamespace(SESSION_REGISTRY=kv)
    await update_kv_session(env, "worker-1", connected=True, hijacked=False)
    assert "session:worker-1" in kv._store
    data = json.loads(kv._store["session:worker-1"])
    assert data["session_id"] == "worker-1"
    assert data["connected"] is True
    assert data["hijacked"] is False


@pytest.mark.asyncio
async def test_update_kv_session_deletes_on_disconnect() -> None:
    kv = _make_kv()
    env = SimpleNamespace(SESSION_REGISTRY=kv)
    await update_kv_session(env, "worker-1", connected=True)
    assert "session:worker-1" in kv._store
    await update_kv_session(env, "worker-1", connected=False)
    assert "session:worker-1" not in kv._store


@pytest.mark.asyncio
async def test_list_kv_sessions_returns_empty_without_binding() -> None:
    env = SimpleNamespace()
    sessions = await list_kv_sessions(env)
    assert sessions == []


@pytest.mark.asyncio
async def test_list_kv_sessions_returns_all_connected() -> None:
    kv = _make_kv()
    env = SimpleNamespace(SESSION_REGISTRY=kv)
    await update_kv_session(env, "w1", connected=True)
    await update_kv_session(env, "w2", connected=True, hijacked=True)
    sessions = await list_kv_sessions(env)
    assert len(sessions) == 2
    ids = {s["session_id"] for s in sessions}
    assert ids == {"w1", "w2"}
    hijacked = next(s for s in sessions if s["session_id"] == "w2")
    assert hijacked["hijacked"] is True


@pytest.mark.asyncio
async def test_get_kv_session_returns_none_without_binding() -> None:
    env = SimpleNamespace()
    result = await get_kv_session(env, "w1")
    assert result is None


@pytest.mark.asyncio
async def test_get_kv_session_returns_none_for_missing_key() -> None:
    kv = _make_kv()
    env = SimpleNamespace(SESSION_REGISTRY=kv)
    result = await get_kv_session(env, "nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_get_kv_session_returns_session_data() -> None:
    kv = _make_kv()
    env = SimpleNamespace(SESSION_REGISTRY=kv)
    await update_kv_session(env, "w1", connected=True, meta={"owner": "alice", "display_name": "Test"})
    result = await get_kv_session(env, "w1")
    assert result is not None
    assert result["session_id"] == "w1"
    assert result["owner"] == "alice"


def _seed_tunnel_entry(kv: SimpleNamespace, tunnel_id: str, **overrides: object) -> None:
    """Seed a KV entry that carries tunnel credential/lifecycle fields."""
    entry = {
        "session_id": tunnel_id,
        "display_name": tunnel_id,
        "connector_type": "tunnel:terminal",
        "worker_token_hash": "wh",
        "share_token_hash": "sh",
        "control_token_hash": "ch",
        "issued_ip": "203.0.113.7",
        "expires_at": 9999999999.0,
        "share_invite_hash": "ih",
        "tunnel_type": "terminal",
    }
    entry.update(overrides)
    kv._store[f"session:{tunnel_id}"] = json.dumps(entry)


@pytest.mark.asyncio
async def test_update_kv_session_preserves_tunnel_credentials() -> None:
    # A status heartbeat (worker connect) must NOT wipe the credential / lifecycle
    # fields the tunnel API wrote: _ensure_credentials reads this same key, so a
    # blind overwrite would null tunnel auth ~60s after every connect.
    kv = _make_kv()
    env = SimpleNamespace(SESSION_REGISTRY=kv)
    _seed_tunnel_entry(kv, "tunnel-1")
    await update_kv_session(env, "tunnel-1", connected=True, hijacked=True)
    data = json.loads(kv._store["session:tunnel-1"])
    # Credentials + lifecycle fields survive...
    assert data["worker_token_hash"] == "wh"
    assert data["share_token_hash"] == "sh"
    assert data["control_token_hash"] == "ch"
    assert data["issued_ip"] == "203.0.113.7"
    assert data["expires_at"] == 9999999999.0
    assert data["share_invite_hash"] == "ih"
    assert data["tunnel_type"] == "terminal"
    # ...while the status fields are updated.
    assert data["connected"] is True
    assert data["hijacked"] is True
    assert data["lifecycle_state"] == "running"


@pytest.mark.asyncio
async def test_update_kv_session_preserves_revocation() -> None:
    # A revoked tunnel (null hashes + revoked flag) must stay revoked across a
    # heartbeat — the merge must never resurrect a cleared credential.
    kv = _make_kv()
    env = SimpleNamespace(SESSION_REGISTRY=kv)
    _seed_tunnel_entry(
        kv,
        "tunnel-2",
        worker_token_hash=None,
        share_token_hash=None,
        control_token_hash=None,
        revoked=True,
    )
    await update_kv_session(env, "tunnel-2", connected=True)
    data = json.loads(kv._store["session:tunnel-2"])
    assert data["revoked"] is True
    assert data["worker_token_hash"] is None
    assert data["share_token_hash"] is None
    assert data["control_token_hash"] is None


@pytest.mark.asyncio
async def test_update_kv_session_ignores_corrupt_existing() -> None:
    kv = _make_kv()
    env = SimpleNamespace(SESSION_REGISTRY=kv)
    kv._store["session:tunnel-3"] = "{not valid json"
    await update_kv_session(env, "tunnel-3", connected=True)
    data = json.loads(kv._store["session:tunnel-3"])
    assert data["session_id"] == "tunnel-3"
    assert data["connected"] is True
    assert "worker_token_hash" not in data


@pytest.mark.asyncio
async def test_update_kv_session_ignores_nondict_existing() -> None:
    kv = _make_kv()
    env = SimpleNamespace(SESSION_REGISTRY=kv)
    kv._store["session:tunnel-4"] = json.dumps(["not", "a", "dict"])
    await update_kv_session(env, "tunnel-4", connected=True)
    data = json.loads(kv._store["session:tunnel-4"])
    assert data["session_id"] == "tunnel-4"


@pytest.mark.asyncio
async def test_update_kv_session_survives_get_error() -> None:
    kv = _make_kv()

    async def boom(_key: str) -> str | None:
        raise RuntimeError("kv get down")

    kv.get = boom
    env = SimpleNamespace(SESSION_REGISTRY=kv)
    # The merge read failing must not stop the status write.
    await update_kv_session(env, "tunnel-5", connected=True)
    data = json.loads(kv._store["session:tunnel-5"])
    assert data["session_id"] == "tunnel-5"
