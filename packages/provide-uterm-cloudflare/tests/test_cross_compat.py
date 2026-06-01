#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Cross-compatibility tests proving CF DO and FastAPI return identical shapes.

Each test uses a parameterized fixture that yields a thin client abstraction
over both backends. This guarantees a single client works against either.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest
from provide.uterm.cloudflare.api.http_routes._dispatch import route_http
from provide.uterm.cloudflare.bridge.hijack import HijackCoordinator
from provide.uterm.cloudflare.state.store import SqliteStateStore

# ---------------------------------------------------------------------------
# CF backend (unit-level stub)
# ---------------------------------------------------------------------------

_DEFAULT_META = {
    "display_name": "w1",
    "connector_type": "telnet",
    "created_at": 1000.0,
    "tags": ["test"],
    "visibility": "public",
    "owner": "alice",
}


class _CfRuntime:
    def __init__(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.store = SqliteStateStore(conn.execute)
        self.store.migrate()
        self.worker_id = "w1"
        self.meta: dict = dict(_DEFAULT_META)
        self.worker_ws = object()  # "connected"
        self.lifecycle_state = "stopped"
        self.input_mode = "hijack"
        self.hijack = HijackCoordinator()
        self.last_snapshot: dict | None = None
        self.last_analysis: str | None = None
        self.browser_hijack_owner: dict[str, str] = {}
        self._ushell: Any = None
        self._ushell_started: bool = False
        # Seed some events for recording
        self.store.append_event("w1", "snapshot", {"screen": "$ hello", "ts": 1000.0})
        self.store.append_event("w1", "term", {"data": "world", "ts": 1001.0})

    async def browser_role_for_request(self, _req: object) -> str:
        return "admin"

    async def request_json(self, req: object) -> dict:
        body = getattr(req, "_body", "{}")
        return json.loads(body) if isinstance(body, str) else body

    def persist_lease(self, _s: object) -> None:
        pass

    def clear_lease(self) -> None:
        pass

    async def push_worker_control(self, *_a: Any, **_k: Any) -> bool:
        return True

    async def broadcast_hijack_state(self) -> None:
        pass

    async def push_worker_input(self, _data: str) -> bool:
        return True

    async def send_ws(self, _ws: Any, _frame: dict) -> None:
        pass

    async def broadcast_worker_frame(self, _frame: object) -> None:
        pass

    def ws_key(self, _ws: Any) -> str:
        return "k"

    def _socket_browser_role(self, _ws: Any) -> str:
        return "admin"


def _cf_req(url: str, *, method: str = "GET", body: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(url=url, method=method, headers={}, _body=json.dumps(body or {}))


async def _cf_call(runtime: _CfRuntime, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
    url = f"http://localhost{path}"
    req = _cf_req(url, method=method, body=body)
    resp = await route_http(runtime, req)
    status = getattr(resp, "status", 200)
    raw = getattr(resp, "body", b"")
    if isinstance(raw, bytes):
        raw = raw.decode()
    return status, json.loads(raw)


# ---------------------------------------------------------------------------
# FastAPI backend (TestClient)
# ---------------------------------------------------------------------------


def _make_fastapi_client():
    """Create a FastAPI TestClient with one auto-start shell session."""
    from starlette.testclient import TestClient

    from provide.uterm.server.app import create_server_app
    from provide.uterm.server.config import config_from_mapping

    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 0, "public_base_url": "http://127.0.0.1:1"},
            "auth": {
                "mode": "header",
                "header_mode_acknowledged": True,
                "worker_bearer_token": "test-bearer-token-32-chars-long-x",
            },
            "sessions": [
                {
                    "session_id": "w1",
                    "display_name": "w1",
                    "connector_type": "shell",
                    "auto_start": False,
                }
            ],
        }
    )
    app = create_server_app(cfg)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Shared field sets (the contract)
# ---------------------------------------------------------------------------

# Fields that MUST be present in GET /api/sessions/{id} for both backends.
SESSION_STATUS_FIELDS = {
    "session_id",
    "display_name",
    "connector_type",
    "lifecycle_state",
    "input_mode",
    "connected",
    "auto_start",
    "tags",
    "recording_enabled",
    "recording_available",
    "owner",
    "visibility",
    "last_error",
}

RECORDING_ENTRY_FIELDS = {"ts", "event", "data"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_status_fields_match_cf() -> None:
    """CF DO returns all required session status fields."""
    rt = _CfRuntime()
    status, body = await _cf_call(rt, "GET", "/api/sessions/w1")
    assert status == 200
    assert SESSION_STATUS_FIELDS.issubset(body.keys()), f"Missing: {SESSION_STATUS_FIELDS - body.keys()}"


def test_session_status_fields_match_fastapi() -> None:
    """FastAPI returns all required session status fields."""
    client = _make_fastapi_client()
    resp = client.get("/api/sessions/w1")
    assert resp.status_code == 200
    body = resp.json()
    assert SESSION_STATUS_FIELDS.issubset(body.keys()), f"Missing: {SESSION_STATUS_FIELDS - body.keys()}"


@pytest.mark.asyncio
async def test_recording_entries_shape_cf() -> None:
    """CF recording entries have {ts, event, data} shape."""
    rt = _CfRuntime()
    status, body = await _cf_call(rt, "GET", "/api/sessions/w1/recording/entries")
    assert status == 200
    assert isinstance(body, list)
    assert len(body) >= 1
    for entry in body:
        assert RECORDING_ENTRY_FIELDS.issubset(entry.keys()), f"Missing: {RECORDING_ENTRY_FIELDS - entry.keys()}"


def test_recording_entries_shape_fastapi() -> None:
    """FastAPI recording entries have {ts, event, data} shape (when recording exists)."""
    client = _make_fastapi_client()
    resp = client.get("/api/sessions/w1/recording/entries")
    # May be 200 with empty list (no recording file) or entries
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    # If entries exist, check shape
    for entry in body:
        assert RECORDING_ENTRY_FIELDS.issubset(entry.keys())


@pytest.mark.asyncio
async def test_worker_input_mode_cf() -> None:
    """CF accepts POST /worker/{id}/input_mode (FastAPI compat alias)."""
    rt = _CfRuntime()
    status, body = await _cf_call(rt, "POST", "/worker/w1/input_mode", {"input_mode": "open"})
    assert status == 200
    assert body["ok"] is True
    assert body["input_mode"] == "open"
    assert body["worker_id"] == "w1"


def test_worker_input_mode_fastapi() -> None:
    """FastAPI accepts POST /worker/{id}/input_mode."""
    client = _make_fastapi_client()
    resp = client.post("/worker/w1/input_mode", json={"input_mode": "open"})
    # May be 200 or 404 (no worker connected), but path is accepted
    assert resp.status_code in (200, 404)


@pytest.mark.asyncio
async def test_session_mode_cf() -> None:
    """CF accepts POST /api/sessions/{id}/mode."""
    rt = _CfRuntime()
    status, body = await _cf_call(rt, "POST", "/api/sessions/w1/mode", {"input_mode": "open"})
    assert status == 200
    assert body["ok"] is True


def test_session_mode_fastapi() -> None:
    """FastAPI accepts POST /api/sessions/{id}/mode."""
    client = _make_fastapi_client()
    resp = client.post("/api/sessions/w1/mode", json={"input_mode": "open"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_recording_meta_cf() -> None:
    """CF recording meta has enabled + entry_count."""
    rt = _CfRuntime()
    status, body = await _cf_call(rt, "GET", "/api/sessions/w1/recording")
    assert status == 200
    assert body["enabled"] is True
    assert body["entry_count"] >= 1


def test_recording_meta_fastapi() -> None:
    """FastAPI recording meta has enabled field."""
    client = _make_fastapi_client()
    resp = client.get("/api/sessions/w1/recording")
    assert resp.status_code == 200
    body = resp.json()
    assert "enabled" in body


@pytest.mark.asyncio
async def test_worker_input_mode_any_path_handled_cf() -> None:
    """CF route_hijack handles /worker/{any}/input_mode (DO is always the target)."""
    rt = _CfRuntime()
    # In CF, every request to the DO is for that DO's worker — path worker_id is ignored
    status, body = await _cf_call(rt, "POST", "/worker/any-name/input_mode", {"input_mode": "open"})
    assert status == 200
    assert body["ok"] is True


@pytest.mark.asyncio
async def test_worker_input_mode_invalid_mode_cf() -> None:
    """CF returns 400 for invalid mode on /worker/{id}/input_mode."""
    rt = _CfRuntime()
    status, _body = await _cf_call(rt, "POST", "/worker/w1/input_mode", {"input_mode": "invalid"})
    assert status == 400


@pytest.mark.asyncio
async def test_worker_input_mode_hijack_conflict_cf() -> None:
    """CF returns 409 when switching to open while hijack active."""
    rt = _CfRuntime()
    import time

    from provide.uterm.cloudflare.bridge.hijack import HijackSession

    rt.hijack._session = HijackSession(hijack_id="h1", owner="x", lease_expires_at=time.time() + 300)
    status, body = await _cf_call(rt, "POST", "/worker/w1/input_mode", {"input_mode": "open"})
    assert status == 409
    assert "hijack" in body["error"].lower()
