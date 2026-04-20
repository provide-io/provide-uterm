"""Tests for the KV session registry (state/registry.py)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from provide.terminal.cloudflare.state.registry import list_kv_sessions, update_kv_session


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
