#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for CF connection profiles CRUD (KV-backed)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from provide.terminal.cloudflare.api._profiles import route_profiles

# ---------------------------------------------------------------------------
# Fake KV store (in-memory dict)
# ---------------------------------------------------------------------------


class _FakeKV:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def put(self, key: str, value: str) -> None:
        self._data[key] = value

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def list(self, prefix: str = "") -> list[dict[str, str]]:
        return [{"name": k} for k in self._data if k.startswith(prefix)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env(kv: _FakeKV | None = None) -> SimpleNamespace:
    return SimpleNamespace(SESSION_REGISTRY=kv or _FakeKV())


def _req(method: str = "GET", body: dict | None = None) -> SimpleNamespace:
    async def _json() -> dict:
        return body or {}

    return SimpleNamespace(method=method, url="http://localhost/api/profiles", json=_json)


async def _call(
    kv: _FakeKV,
    method: str,
    path: str,
    body: dict | None = None,
    principal: str = "alice",
) -> tuple[int, Any]:
    req = _req(method, body)
    env = _env(kv)
    resp = await route_profiles(req, env, path, method, principal)
    status = getattr(resp, "status", 200)
    raw = getattr(resp, "body", "")
    if isinstance(raw, bytes):
        raw = raw.decode()
    return status, json.loads(raw)


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_profile() -> None:
    kv = _FakeKV()
    status, body = await _call(
        kv, "POST", "/api/profiles", {"name": "My SSH", "connector_type": "ssh", "host": "example.com"}
    )
    assert status == 200
    assert body["name"] == "My SSH"
    assert body["connector_type"] == "ssh"
    assert body["host"] == "example.com"
    assert body["owner"] == "alice"
    assert body["profile_id"].startswith("profile-")
    assert body["created_at"] > 0
    assert body["updated_at"] > 0


@pytest.mark.asyncio
async def test_create_profile_defaults() -> None:
    kv = _FakeKV()
    status, body = await _call(kv, "POST", "/api/profiles", {})
    assert status == 200
    assert body["name"] == "Unnamed"
    assert body["connector_type"] == "ssh"
    assert body["input_mode"] == "open"
    assert body["visibility"] == "private"
    assert body["tags"] == []


@pytest.mark.asyncio
async def test_create_profile_invalid_connector_type() -> None:
    kv = _FakeKV()
    status, body = await _call(kv, "POST", "/api/profiles", {"connector_type": "invalid"})
    assert status == 422


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_profiles_empty() -> None:
    kv = _FakeKV()
    status, body = await _call(kv, "GET", "/api/profiles")
    assert status == 200
    assert body == []


@pytest.mark.asyncio
async def test_list_profiles_returns_own() -> None:
    kv = _FakeKV()
    await _call(kv, "POST", "/api/profiles", {"name": "P1"})
    status, body = await _call(kv, "GET", "/api/profiles")
    assert status == 200
    assert len(body) == 1
    assert body[0]["name"] == "P1"


@pytest.mark.asyncio
async def test_list_profiles_excludes_others_private() -> None:
    kv = _FakeKV()
    await _call(kv, "POST", "/api/profiles", {"name": "Alice's"}, principal="alice")
    status, body = await _call(kv, "GET", "/api/profiles", principal="bob")
    assert status == 200
    assert len(body) == 0  # bob can't see alice's private profile


@pytest.mark.asyncio
async def test_list_profiles_includes_shared() -> None:
    kv = _FakeKV()
    await _call(kv, "POST", "/api/profiles", {"name": "Shared", "visibility": "shared"}, principal="alice")
    status, body = await _call(kv, "GET", "/api/profiles", principal="bob")
    assert status == 200
    assert len(body) == 1
    assert body[0]["name"] == "Shared"


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_profile() -> None:
    kv = _FakeKV()
    _, created = await _call(kv, "POST", "/api/profiles", {"name": "Test"})
    pid = created["profile_id"]
    status, body = await _call(kv, "GET", f"/api/profiles/{pid}")
    assert status == 200
    assert body["name"] == "Test"


@pytest.mark.asyncio
async def test_get_profile_not_found() -> None:
    kv = _FakeKV()
    status, _body = await _call(kv, "GET", "/api/profiles/nonexistent")
    assert status == 404


@pytest.mark.asyncio
async def test_get_profile_forbidden() -> None:
    kv = _FakeKV()
    _, created = await _call(kv, "POST", "/api/profiles", {"name": "Private"}, principal="alice")
    pid = created["profile_id"]
    status, _body = await _call(kv, "GET", f"/api/profiles/{pid}", principal="bob")
    assert status == 403


# ---------------------------------------------------------------------------
# UPDATE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_profile() -> None:
    kv = _FakeKV()
    _, created = await _call(kv, "POST", "/api/profiles", {"name": "Old"})
    pid = created["profile_id"]
    status, body = await _call(kv, "PUT", f"/api/profiles/{pid}", {"name": "New", "host": "new.example.com"})
    assert status == 200
    assert body["name"] == "New"
    assert body["host"] == "new.example.com"
    assert body["updated_at"] >= created["updated_at"]


@pytest.mark.asyncio
async def test_update_profile_not_found() -> None:
    kv = _FakeKV()
    status, _body = await _call(kv, "PUT", "/api/profiles/nonexistent", {"name": "x"})
    assert status == 404


@pytest.mark.asyncio
async def test_update_profile_forbidden() -> None:
    kv = _FakeKV()
    _, created = await _call(kv, "POST", "/api/profiles", {"name": "Alice's"}, principal="alice")
    pid = created["profile_id"]
    status, _body = await _call(kv, "PUT", f"/api/profiles/{pid}", {"name": "Hacked"}, principal="bob")
    assert status == 403


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_profile() -> None:
    kv = _FakeKV()
    _, created = await _call(kv, "POST", "/api/profiles", {"name": "ToDelete"})
    pid = created["profile_id"]
    status, body = await _call(kv, "DELETE", f"/api/profiles/{pid}")
    assert status == 200
    assert body["ok"] is True
    # Verify it's gone
    status2, _body2 = await _call(kv, "GET", f"/api/profiles/{pid}")
    assert status2 == 404


@pytest.mark.asyncio
async def test_delete_profile_not_found() -> None:
    kv = _FakeKV()
    status, _body = await _call(kv, "DELETE", "/api/profiles/nonexistent")
    assert status == 404


@pytest.mark.asyncio
async def test_delete_profile_forbidden() -> None:
    kv = _FakeKV()
    _, created = await _call(kv, "POST", "/api/profiles", {"name": "Alice's"}, principal="alice")
    pid = created["profile_id"]
    status, _body = await _call(kv, "DELETE", f"/api/profiles/{pid}", principal="bob")
    assert status == 403


# ---------------------------------------------------------------------------
# CONNECT FROM PROFILE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_from_profile() -> None:
    kv = _FakeKV()
    _, created = await _call(
        kv,
        "POST",
        "/api/profiles",
        {
            "name": "SSH Prod",
            "connector_type": "ssh",
            "host": "prod.example.com",
            "port": 22,
            "username": "deploy",
        },
    )
    pid = created["profile_id"]
    status, body = await _call(kv, "POST", f"/api/profiles/{pid}/connect", {"password": "secret"})
    assert status == 200
    assert body["session_id"].startswith("connect-")
    assert body["display_name"] == "SSH Prod"
    assert body["connector_type"] == "ssh"
    assert "url" in body
    # Password must NOT be stored in the session entry
    session_raw = await kv.get(f"session:{body['session_id']}")
    assert session_raw is not None
    session = json.loads(session_raw)
    assert "password" not in json.dumps(session)


@pytest.mark.asyncio
async def test_connect_from_profile_not_found() -> None:
    kv = _FakeKV()
    status, _body = await _call(kv, "POST", "/api/profiles/nonexistent/connect")
    assert status == 404


@pytest.mark.asyncio
async def test_connect_from_profile_forbidden() -> None:
    kv = _FakeKV()
    _, created = await _call(kv, "POST", "/api/profiles", {"name": "Private"}, principal="alice")
    pid = created["profile_id"]
    status, _body = await _call(kv, "POST", f"/api/profiles/{pid}/connect", principal="bob")
    assert status == 403


@pytest.mark.asyncio
async def test_connect_from_shared_profile() -> None:
    kv = _FakeKV()
    _, created = await _call(kv, "POST", "/api/profiles", {"name": "Shared", "visibility": "shared"}, principal="alice")
    pid = created["profile_id"]
    status, body = await _call(kv, "POST", f"/api/profiles/{pid}/connect", principal="bob")
    assert status == 200
    assert body["owner"] == "bob"  # session owned by the caller, not the profile owner


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profiles_no_kv() -> None:
    env = SimpleNamespace()  # no SESSION_REGISTRY
    req = _req()
    resp = await route_profiles(req, env, "/api/profiles", "GET", "alice")
    assert getattr(resp, "status", 200) == 500


@pytest.mark.asyncio
async def test_connect_without_session_kv() -> None:
    """_connect still returns OK even if SESSION_REGISTRY disappears mid-call."""
    from provide.terminal.cloudflare.api._profiles import _connect

    kv = _FakeKV()
    # Create a profile directly in KV
    await kv.put(
        "profile:p1",
        json.dumps(
            {
                "profile_id": "p1",
                "owner": "alice",
                "name": "x",
                "connector_type": "ssh",
                "host": "h",
                "port": 22,
                "username": "u",
                "tags": [],
                "input_mode": "open",
                "visibility": "private",
            }
        ),
    )
    # Call _connect with an env that has no SESSION_REGISTRY attr
    env_no_kv = SimpleNamespace()

    async def _json() -> dict:
        return {}

    req = SimpleNamespace(json=_json)
    resp = await _connect(req, env_no_kv, kv, "p1", "alice")
    status = getattr(resp, "status", 200)
    assert status == 200  # session created but not written to KV


@pytest.mark.asyncio
async def test_profiles_unknown_path() -> None:
    kv = _FakeKV()
    status, _body = await _call(kv, "GET", "/api/profiles/")
    assert status == 404


@pytest.mark.asyncio
async def test_profiles_unknown_sub_path() -> None:
    """PUT /api/profiles/{id}/something returns 404."""
    kv = _FakeKV()
    _, created = await _call(kv, "POST", "/api/profiles", {"name": "x"})
    pid = created["profile_id"]
    status, _body = await _call(kv, "PATCH", f"/api/profiles/{pid}/unknown")
    assert status == 404


@pytest.mark.asyncio
async def test_create_profile_bad_json() -> None:
    """POST with unparsable body uses defaults."""
    kv = _FakeKV()

    async def _bad_json() -> dict:
        raise ValueError("bad json")

    env = _env(kv)
    req = SimpleNamespace(method="POST", url="http://localhost/api/profiles", json=_bad_json)
    resp = await route_profiles(req, env, "/api/profiles", "POST", "alice")
    status = getattr(resp, "status", 200)
    assert status == 200  # falls back to empty body → defaults


@pytest.mark.asyncio
async def test_update_profile_bad_json() -> None:
    """PUT with unparsable body keeps existing values."""
    kv = _FakeKV()
    _, created = await _call(kv, "POST", "/api/profiles", {"name": "Original"})
    pid = created["profile_id"]

    async def _bad_json() -> dict:
        raise ValueError("bad json")

    env = _env(kv)
    req = SimpleNamespace(method="PUT", url=f"http://localhost/api/profiles/{pid}", json=_bad_json)
    resp = await route_profiles(req, env, f"/api/profiles/{pid}", "PUT", "alice")
    body = json.loads(getattr(resp, "body", b""))
    assert body["name"] == "Original"  # unchanged


@pytest.mark.asyncio
async def test_connect_bad_json() -> None:
    """POST connect with unparsable body still works (no password)."""
    kv = _FakeKV()
    _, created = await _call(kv, "POST", "/api/profiles", {"name": "x", "connector_type": "ssh"})
    pid = created["profile_id"]

    async def _bad_json() -> dict:
        raise ValueError("bad json")

    env = _env(kv)
    req = SimpleNamespace(method="POST", url=f"http://localhost/api/profiles/{pid}/connect", json=_bad_json)
    resp = await route_profiles(req, env, f"/api/profiles/{pid}/connect", "POST", "alice")
    status = getattr(resp, "status", 200)
    assert status == 200


@pytest.mark.asyncio
async def test_list_profiles_skips_deleted_kv_entries() -> None:
    """KV list returns keys but get returns None for deleted entries."""
    kv = _FakeKV()
    # Manually put a key that get() will return None for (simulate race)
    kv._data["profile:orphan"] = '{"owner":"alice","visibility":"private"}'
    await kv.delete("profile:orphan")
    # Add the key back to the list but not the data (by manipulating internal state)
    kv._data["profile:corrupt"] = "not-valid-json{{"  # corrupt → json.loads will fail
    # Also simulate a key in list that returns None from get (race condition)
    original_get = kv.get

    async def _get_with_ghost(key: str) -> str | None:
        if key == "profile:ghost":
            return None
        return await original_get(key)

    kv.get = _get_with_ghost
    kv._data["profile:ghost"] = "placeholder"  # in list but get returns None
    status, body = await _call(kv, "GET", "/api/profiles")
    assert status == 200
