# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for POST /api/pam-events CF Worker endpoint."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import provide.uterm.cloudflare.cf_types  # noqa: F401  — loads fallback classes


def _env(kv: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(SESSION_REGISTRY=kv, AUTH_MODE="dev")


def _req(body: dict | None = None, method: str = "POST") -> SimpleNamespace:
    data = json.dumps(body or {})

    async def _json() -> SimpleNamespace:
        import json as _j

        return _j.loads(data)

    return SimpleNamespace(
        url="https://x/api/pam-events",
        method=method,
        headers=SimpleNamespace(get=lambda k, d=None: d),
        json=_json,
    )


# ── handle_pam_event ──────────────────────────────────────────────────────────


async def test_pam_event_wrong_method_returns_405() -> None:
    from provide.uterm.cloudflare.api._pam import handle_pam_event

    resp = await handle_pam_event(_req(method="GET"), _env())
    assert resp.status == 405


async def test_pam_event_bad_json_returns_400() -> None:
    from provide.uterm.cloudflare.api._pam import handle_pam_event

    async def _bad_json():
        raise ValueError("not json")

    req = SimpleNamespace(
        url="https://x/api/pam-events",
        method="POST",
        headers=SimpleNamespace(get=lambda k, d=None: d),
        json=_bad_json,
    )
    resp = await handle_pam_event(req, _env())
    assert resp.status == 400


async def test_pam_event_unknown_event_type_returns_422() -> None:
    from provide.uterm.cloudflare.api._pam import handle_pam_event

    req = _req({"event": "reboot", "username": "alice", "tty": "/dev/pts/3", "pid": 1})
    resp = await handle_pam_event(req, _env())
    assert resp.status == 422


async def test_pam_event_missing_username_returns_422() -> None:
    from provide.uterm.cloudflare.api._pam import handle_pam_event

    req = _req({"event": "open", "username": "", "tty": "/dev/pts/3", "pid": 1})
    resp = await handle_pam_event(req, _env())
    assert resp.status == 422


async def test_pam_event_open_writes_to_kv() -> None:
    from provide.uterm.cloudflare.api._pam import handle_pam_event

    kv = AsyncMock()
    kv.put = AsyncMock()
    req = _req({"event": "open", "username": "alice", "tty": "/dev/pts/3", "pid": 123, "mode": "notify"})
    resp = await handle_pam_event(req, _env(kv))

    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["ok"] is True
    assert body["action"] == "created"
    assert body["session_id"] == "pam-alice-3"
    kv.put.assert_awaited_once()
    key, val = kv.put.call_args[0]
    assert key == "session:pam-alice-3"
    session_data = json.loads(val)
    assert session_data["session_id"] == "pam-alice-3"
    assert session_data["owner"] == "alice"
    assert "pam" in session_data["tags"]


async def test_pam_event_open_no_kv_succeeds() -> None:
    """No KV binding configured — should still return 200."""
    from provide.uterm.cloudflare.api._pam import handle_pam_event

    req = _req({"event": "open", "username": "bob", "tty": "/dev/pts/7", "pid": 99})
    resp = await handle_pam_event(req, _env(kv=None))
    assert resp.status == 200


async def test_pam_event_close_deletes_from_kv() -> None:
    from provide.uterm.cloudflare.api._pam import handle_pam_event

    kv = AsyncMock()
    kv.delete = AsyncMock()
    req = _req({"event": "close", "username": "alice", "tty": "/dev/pts/3", "pid": 123})
    resp = await handle_pam_event(req, _env(kv))

    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["action"] == "deleted"
    assert body["session_id"] == "pam-alice-3"
    kv.delete.assert_awaited_once_with("session:pam-alice-3")


async def test_pam_event_close_no_kv_succeeds() -> None:
    from provide.uterm.cloudflare.api._pam import handle_pam_event

    req = _req({"event": "close", "username": "alice", "tty": "/dev/pts/3", "pid": 1})
    resp = await handle_pam_event(req, _env(kv=None))
    assert resp.status == 200


async def test_pam_event_visibility_is_operator() -> None:
    from provide.uterm.cloudflare.api._pam import handle_pam_event

    kv = AsyncMock()
    req = _req({"event": "open", "username": "carol", "tty": "/dev/pts/1", "pid": 5})
    await handle_pam_event(req, _env(kv))

    _, val = kv.put.call_args[0]
    session_data = json.loads(val)
    assert session_data["visibility"] == "operator"


async def test_pam_event_tty_empty_uses_tty_fallback() -> None:
    from provide.uterm.cloudflare.api._pam import handle_pam_event

    kv = AsyncMock()
    req = _req({"event": "open", "username": "dave", "tty": "", "pid": 2})
    resp = await handle_pam_event(req, _env(kv))

    body = json.loads(resp.body)
    assert body["session_id"] == "pam-dave-tty"


# ── entry.py route wiring ─────────────────────────────────────────────────────


async def test_route_http_dispatches_pam_events() -> None:
    """entry.py must route /api/pam-events to the pam handler."""
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.entry import Default

    env = SimpleNamespace(
        AUTH_MODE="jwt",
        JWT_ALGORITHMS="HS256",
        JWT_PUBLIC_KEY_PEM="test-secret-key-32-bytes-minimum!",
        WORKER_BEARER_TOKEN="t",
        SESSION_REGISTRY=None,
    )
    default = Default(env)
    # Exercise the legacy open-access path (reachable only via direct config mutation).
    default._config = CloudflareConfig.from_env(env)
    default._config.jwt.mode = "dev"

    async def _json() -> dict:
        return {"event": "open", "username": "alice", "tty": "/dev/pts/3", "pid": 1}

    req = SimpleNamespace(
        url="https://x/api/pam-events",
        method="POST",
        headers=SimpleNamespace(get=lambda k, d=None: "Bearer dev-token" if k == "Authorization" else d),
        json=_json,
    )

    resp = await default.fetch(req)
    # Should reach the handler (200) rather than fall through to 404
    assert resp.status == 200
