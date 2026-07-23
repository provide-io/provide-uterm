#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage tests for entry.py and state/registry.py."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Ensure cf_types fallback classes (Response, WorkerEntrypoint, DurableObject) are
# loaded before entry.py is imported — entry.py's module-level class definition
# ``class Default(WorkerEntrypoint)`` needs WorkerEntrypoint to be non-None.
import provide.uterm.cloudflare.cf_types  # noqa: F401

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_default(env_attrs: dict | None = None):
    from provide.uterm.cloudflare.entry import Default

    attrs: dict = {"AUTH_MODE": "dev"}
    if env_attrs:
        attrs.update(env_attrs)
    return Default(SimpleNamespace(**attrs))


def _req(path: str, method: str = "GET", headers: dict | None = None) -> SimpleNamespace:
    hdr = headers or {}

    def _get(k, default=None):
        return hdr.get(k, default)

    return SimpleNamespace(url=f"https://x{path}", method=method, headers=SimpleNamespace(get=_get))


# ---------------------------------------------------------------------------
# _resolve_spa_route (lines 129-141)
# ---------------------------------------------------------------------------


def test_has_cf_service_token_exception_handling() -> None:
    from provide.uterm.cloudflare.entry.auth import _has_cf_service_token

    class _Bad:
        @property
        def headers(self):
            raise RuntimeError("boom")

    assert _has_cf_service_token(_Bad()) is False


# ---------------------------------------------------------------------------
# _handle_connect (lines 245-281)
# ---------------------------------------------------------------------------


async def test_handle_connect_post_creates_session() -> None:
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry.handlers import _handle_connect

    async def _json():
        return {"connector_type": "telnet", "display_name": "My Session", "input_mode": "open"}

    req = SimpleNamespace(method="POST", json=_json)
    kv = AsyncMock()
    env = SimpleNamespace(SESSION_REGISTRY=kv)
    resp = await _handle_connect(req, env, CloudflareConfig())
    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["connector_type"] == "telnet"
    assert data["display_name"] == "My Session"
    assert data["input_mode"] == "open"
    assert data["url"].startswith("/app/session/connect-")
    kv.put.assert_awaited_once()


async def test_handle_connect_non_post_returns_405() -> None:
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry.handlers import _handle_connect

    resp = await _handle_connect(SimpleNamespace(method="GET"), SimpleNamespace(), CloudflareConfig())
    assert resp.status == 405


async def test_handle_connect_ushell_prefix() -> None:
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry.handlers import _handle_connect

    async def _json():
        return {"connector_type": "ushell"}

    kv = AsyncMock()
    resp = await _handle_connect(
        SimpleNamespace(method="POST", json=_json), SimpleNamespace(SESSION_REGISTRY=kv), CloudflareConfig()
    )
    assert json.loads(resp.body)["session_id"].startswith("ushell-")


async def test_handle_connect_no_kv_returns_500() -> None:
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry.handlers import _handle_connect

    async def _json():
        return {}

    resp = await _handle_connect(
        SimpleNamespace(method="POST", json=_json), SimpleNamespace(SESSION_REGISTRY=None), CloudflareConfig()
    )
    assert resp.status == 500


async def test_handle_connect_bad_json_uses_defaults() -> None:
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry.handlers import _handle_connect

    async def _json():
        raise ValueError("bad json")

    kv = AsyncMock()
    resp = await _handle_connect(
        SimpleNamespace(method="POST", json=_json), SimpleNamespace(SESSION_REGISTRY=kv), CloudflareConfig()
    )
    assert json.loads(resp.body)["connector_type"] == "shell"


async def test_handle_connect_dev_mode_sets_public_no_owner() -> None:
    """In dev/none mode, quick-connect sessions must be public with no owner."""
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry.handlers import _handle_connect

    async def _json():
        return {}

    kv = AsyncMock()
    cfg = CloudflareConfig()  # defaults to dev/none
    resp = await _handle_connect(
        SimpleNamespace(method="POST", json=_json, headers=SimpleNamespace(get=lambda k, d=None: None)),
        SimpleNamespace(SESSION_REGISTRY=kv),
        cfg,
    )
    data = json.loads(resp.body)
    assert data["owner"] is None
    assert data["visibility"] == "public"


# ---------------------------------------------------------------------------
# _handle_sessions DELETE (lines 227-238)
# ---------------------------------------------------------------------------


async def test_handle_sessions_delete_purges_kv() -> None:
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry.handlers import _handle_sessions

    kv = AsyncMock()
    kv.list.return_value = SimpleNamespace(keys=[SimpleNamespace(name="session:abc")])
    resp = await _handle_sessions(
        SimpleNamespace(method="DELETE"), SimpleNamespace(SESSION_REGISTRY=kv), CloudflareConfig()
    )
    data = json.loads(resp.body)
    assert data["ok"] is True and data["deleted"] == 1
    # Must filter to session: prefix so profile keys are not deleted.
    kv.list.assert_awaited_once_with(prefix="session:")


async def test_handle_sessions_delete_preserves_profile_keys() -> None:
    """Bulk delete must only remove session:* keys, not profile:* keys."""
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry.handlers import _handle_sessions

    deleted: list[str] = []

    class _FakeKV:
        async def list(self, *, prefix: str = "") -> object:
            # Simulate registry containing both session and profile keys;
            # only return keys matching the given prefix.
            all_keys = ["session:s1", "session:s2", "profile:p1"]
            return SimpleNamespace(keys=[SimpleNamespace(name=k) for k in all_keys if k.startswith(prefix)])

        async def delete(self, key: str) -> None:
            deleted.append(key)

    await _handle_sessions(
        SimpleNamespace(method="DELETE"), SimpleNamespace(SESSION_REGISTRY=_FakeKV()), CloudflareConfig()
    )
    assert sorted(deleted) == ["session:s1", "session:s2"]
    assert "profile:p1" not in deleted


async def test_handle_sessions_delete_no_kv_returns_500() -> None:
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry.handlers import _handle_sessions

    resp = await _handle_sessions(SimpleNamespace(method="DELETE"), SimpleNamespace(), CloudflareConfig())
    assert resp.status == 500


async def test_handle_sessions_delete_non_admin_returns_403() -> None:
    """Bulk DELETE requires admin role; non-admin principals receive 403."""
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry.handlers import _handle_sessions

    with patch(
        "provide.uterm.cloudflare.entry.auth._decode_jwt_principal",
        new=AsyncMock(return_value=SimpleNamespace(subject_id="bob", roles=("viewer",))),
    ):
        resp = await _handle_sessions(SimpleNamespace(method="DELETE"), SimpleNamespace(), CloudflareConfig())
    assert resp.status == 403
    assert "admin role required" in json.loads(resp.body)["error"]
