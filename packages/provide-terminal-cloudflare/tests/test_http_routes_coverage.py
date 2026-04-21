#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage tests for api/http_routes.py — error/auth branches not covered by contract tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

from provide.terminal.cloudflare.api.http_routes import route_http
from provide.terminal.cloudflare.api.http_routes._shared import _extract_prompt_id
from provide.terminal.cloudflare.bridge.hijack import HijackCoordinator

# ---------------------------------------------------------------------------
# Shared helpers (same pattern as test_api_contracts.py)
# ---------------------------------------------------------------------------


class _Req:
    def __init__(self, url: str, *, method: str = "GET"):
        self.url = url
        self.method = method
        self._body = "{}"

    def with_body(self, data: dict) -> _Req:
        self._body = json.dumps(data)
        return self


class _Runtime:
    def __init__(
        self,
        *,
        role: str = "admin",
        worker_ws: object | None = None,
        browser_role: str = "admin",
        worker_id: str = "w",
    ) -> None:
        self.worker_id = worker_id
        self.meta: dict = {
            "display_name": self.worker_id,
            "connector_type": "unknown",
            "created_at": 0.0,
            "tags": [],
            "visibility": "public",
            "owner": None,
        }
        self.worker_ws = worker_ws
        self.hijack = HijackCoordinator()
        self._role = role
        self._browser_role = browser_role
        self.last_snapshot: dict | None = None
        self.last_analysis: str | None = None
        self.lifecycle_state = "stopped"
        self.input_mode: str = "hijack"
        self.browser_hijack_owner: dict[str, str] = {}
        self.browser_resume_tokens: dict[str, str] = {}

    async def request_json(self, request: object) -> dict:
        return json.loads(getattr(request, "_body", "{}"))

    async def browser_role_for_request(self, request: object) -> str:
        return self._role

    def persist_lease(self, session: object) -> None:
        pass

    def clear_lease(self) -> None:
        pass

    async def push_worker_control(self, action: str, *, owner: str, lease_s: int) -> bool:
        return self.worker_ws is not None

    async def broadcast_hijack_state(self) -> None:
        pass

    async def push_worker_input(self, data: str) -> bool:
        return self.worker_ws is not None

    async def send_ws(self, ws: object, frame: dict) -> None:
        pass

    def ws_key(self, ws: object) -> str:
        return str(id(ws))

    def _socket_browser_role(self, ws: object) -> str:
        return self._browser_role

    @property
    def store(self) -> object:
        return SimpleNamespace(
            list_events_since=lambda *_a, **_k: [],
            load_session=lambda *_a, **_k: None,
            current_event_seq=lambda *_a, **_k: 0,
            min_event_seq=lambda *_a, **_k: 0,
            save_input_mode=lambda *_a, **_k: None,
        )


def _body(resp: object) -> dict:
    return json.loads(getattr(resp, "body", "{}") or "{}")


# ---------------------------------------------------------------------------
# _extract_prompt_id — lines 44-46
# ---------------------------------------------------------------------------


def test_extract_prompt_id_returns_id_when_present() -> None:
    """Lines 44-46: prompt_detected dict with string prompt_id → returns it."""
    snap = {"prompt_detected": {"prompt_id": "login-prompt"}}
    assert _extract_prompt_id(snap) == "login-prompt"


def test_extract_prompt_id_returns_none_when_empty_string() -> None:
    snap = {"prompt_detected": {"prompt_id": ""}}
    assert _extract_prompt_id(snap) is None


def test_extract_prompt_id_returns_none_when_no_snapshot() -> None:
    assert _extract_prompt_id(None) is None


# ---------------------------------------------------------------------------
# GET /api/health — line 56
# ---------------------------------------------------------------------------


async def test_health_endpoint() -> None:
    """Line 56: /api/health returns ok=True."""
    runtime = _Runtime()
    resp = await route_http(runtime, _Req("https://example.invalid/api/health"))
    assert resp.status == 200
    data = _body(resp)
    assert data["ok"] is True
    assert "provide-terminal" in data.get("service", "")


# ---------------------------------------------------------------------------
# POST /hijack/acquire — 403 (non-admin) and 409 (conflict)
# ---------------------------------------------------------------------------


async def test_acquire_403_non_admin() -> None:
    """Line 82: non-admin role → 403."""
    runtime = _Runtime(role="viewer")
    req = _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "alice", "lease_s": 60})
    resp = await route_http(runtime, req)
    assert resp.status == 403


async def test_acquire_409_conflict() -> None:
    """Line 90: second acquire by a different owner while one is active → 409."""
    runtime = _Runtime()
    req1 = _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "alice", "lease_s": 60})
    await route_http(runtime, req1)
    req2 = _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "bob", "lease_s": 60})
    resp = await route_http(runtime, req2)
    assert resp.status == 409


# ---------------------------------------------------------------------------
# POST /hijack/{id}/heartbeat — 403, 404, 400, 409
# ---------------------------------------------------------------------------


async def test_heartbeat_403_non_admin() -> None:
    """Line 106: non-admin → 403."""
    runtime = _Runtime(role="viewer")
    req = _Req("https://x/worker/w/hijack/abc/heartbeat", method="POST")
    resp = await route_http(runtime, req)
    assert resp.status == 403


async def test_heartbeat_404_no_hijack_id() -> None:
    """Line 109: path has no parseable hijack_id → 404."""
    runtime = _Runtime()
    req = _Req("https://x/worker/w/hijack/heartbeat", method="POST")
    resp = await route_http(runtime, req)
    assert resp.status == 404


async def test_heartbeat_400_invalid_lease() -> None:
    """Line 113: non-integer lease_s → 400."""
    runtime = _Runtime()
    r1 = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "a", "lease_s": 60}),
    )
    hid = _body(r1)["hijack_id"]
    req = _Req(f"https://x/worker/w/hijack/{hid}/heartbeat", method="POST").with_body({"lease_s": "notanumber"})
    resp = await route_http(runtime, req)
    assert resp.status == 400


async def test_heartbeat_409_wrong_id() -> None:
    """Line 116: heartbeat with wrong hijack_id → 409."""
    runtime = _Runtime()
    await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "a", "lease_s": 60}),
    )
    req = _Req("https://x/worker/w/hijack/aaaaaaaa-0000-0000-0000-000000000000/heartbeat", method="POST").with_body(
        {"lease_s": 60}
    )
    resp = await route_http(runtime, req)
    assert resp.status == 409


# ---------------------------------------------------------------------------
# POST /hijack/{id}/release — 403, 404, 409
# ---------------------------------------------------------------------------


async def test_release_403_non_admin() -> None:
    """Line 130: non-admin → 403."""
    runtime = _Runtime(role="viewer")
    resp = await route_http(runtime, _Req("https://x/worker/w/hijack/abc/release", method="POST"))
    assert resp.status == 403


async def test_release_404_no_hijack_id() -> None:
    """Line 133: path with no parseable id → 404."""
    runtime = _Runtime()
    resp = await route_http(runtime, _Req("https://x/worker/w/hijack/release", method="POST"))
    assert resp.status == 404


async def test_release_409_wrong_id() -> None:
    """Line 136: release with wrong hijack_id → 409."""
    runtime = _Runtime()
    await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "a", "lease_s": 60}),
    )
    resp = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/aaaaaaaa-0000-0000-0000-000000000000/release", method="POST"),
    )
    assert resp.status == 409


# ---------------------------------------------------------------------------
# POST /hijack/{id}/step — 403, 404, 403 (not owner), 409 (no worker)
# ---------------------------------------------------------------------------


async def test_step_403_non_admin() -> None:
    """Line 144: non-admin → 403."""
    runtime = _Runtime(role="viewer")
    resp = await route_http(runtime, _Req("https://x/worker/w/hijack/abc/step", method="POST"))
    assert resp.status == 403


async def test_step_404_no_hijack_id() -> None:
    """Line 147: no parseable hijack_id → 404."""
    runtime = _Runtime()
    resp = await route_http(runtime, _Req("https://x/worker/w/hijack/step", method="POST"))
    assert resp.status == 404


async def test_step_403_not_owner() -> None:
    """Line 149: step called with wrong hijack_id (not owner) → 403."""
    runtime = _Runtime()
    await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "alice", "lease_s": 60}),
    )
    # Use a different hijack_id that doesn't match the active session
    resp = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/aaaaaaaa-0000-0000-0000-000000000000/step", method="POST"),
    )
    assert resp.status == 403


async def test_step_409_no_worker() -> None:
    """Line 153: step succeeds auth but push_worker_control returns False → 409."""
    runtime = _Runtime(worker_ws=None)  # no worker → push returns False
    r1 = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "a", "lease_s": 60}),
    )
    hid = _body(r1)["hijack_id"]
    resp = await route_http(runtime, _Req(f"https://x/worker/w/hijack/{hid}/step", method="POST"))
    assert resp.status == 409


# ---------------------------------------------------------------------------
# POST /hijack/{id}/send — 403, 404, 400, 403 (not owner), 409
# ---------------------------------------------------------------------------


async def test_send_403_non_admin() -> None:
    """Line 166: non-admin → 403."""
    runtime = _Runtime(role="viewer")
    resp = await route_http(runtime, _Req("https://x/worker/w/hijack/abc/send", method="POST"))
    assert resp.status == 403


async def test_send_404_no_hijack_id() -> None:
    """Line 169: no parseable hijack_id → 404."""
    runtime = _Runtime()
    resp = await route_http(runtime, _Req("https://x/worker/w/hijack/send", method="POST"))
    assert resp.status == 404


async def test_send_400_empty_keys() -> None:
    """Line 173: empty keys payload → 400."""
    runtime = _Runtime()
    r1 = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "a", "lease_s": 60}),
    )
    hid = _body(r1)["hijack_id"]
    resp = await route_http(
        runtime,
        _Req(f"https://x/worker/w/hijack/{hid}/send", method="POST").with_body({"keys": ""}),
    )
    assert resp.status == 400


async def test_send_400_keys_too_long() -> None:
    """Line 180: keys payload exceeds _MAX_INPUT_CHARS → 400."""
    runtime = _Runtime()
    r1 = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "a", "lease_s": 60}),
    )
    hid = _body(r1)["hijack_id"]
    resp = await route_http(
        runtime,
        _Req(f"https://x/worker/w/hijack/{hid}/send", method="POST").with_body({"keys": "x" * 10_001}),
    )
    assert resp.status == 400
    assert _body(resp)["error"] == "keys too long"


async def test_send_403_not_owner() -> None:
    """Line 175: send with wrong hijack_id (not owner) → 403."""
    runtime = _Runtime()
    await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "a", "lease_s": 60}),
    )
    resp = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/aaaaaaaa-0000-0000-0000-000000000000/send", method="POST").with_body(
            {"keys": "ls\r"}
        ),
    )
    assert resp.status == 403


async def test_send_409_no_worker() -> None:
    """Line 178: send succeeds auth but push_worker_input returns False → 409."""
    runtime = _Runtime(worker_ws=None)
    r1 = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "a", "lease_s": 60}),
    )
    hid = _body(r1)["hijack_id"]
    resp = await route_http(
        runtime,
        _Req(f"https://x/worker/w/hijack/{hid}/send", method="POST").with_body({"keys": "ls\r"}),
    )
    assert resp.status == 409


# ---------------------------------------------------------------------------
# GET /hijack/{id}/snapshot — 403, 404
# ---------------------------------------------------------------------------


async def test_snapshot_403_non_admin() -> None:
    """Line 193: non-admin → 403."""
    runtime = _Runtime(role="viewer")
    resp = await route_http(runtime, _Req("https://x/worker/w/hijack/abc/snapshot"))
    assert resp.status == 403


async def test_snapshot_404_no_hijack_id() -> None:
    """Line 196: no parseable hijack_id → 404."""
    runtime = _Runtime()
    resp = await route_http(runtime, _Req("https://x/worker/w/hijack/snapshot"))
    assert resp.status == 404


# ---------------------------------------------------------------------------
# GET /hijack/{id}/events — 404, after_seq parse error
# ---------------------------------------------------------------------------


async def test_events_404_no_hijack_id() -> None:
    """Line 218: no parseable hijack_id → 404."""
    runtime = _Runtime()
    resp = await route_http(runtime, _Req("https://x/worker/w/hijack/events"))
    assert resp.status == 404


async def test_events_bad_after_seq_defaults_to_zero() -> None:
    """Lines 221-222: invalid after_seq query param → defaults to 0, still returns 200."""
    runtime = _Runtime()
    r1 = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "a", "lease_s": 60}),
    )
    hid = _body(r1)["hijack_id"]
    resp = await route_http(
        runtime,
        _Req(f"https://x/worker/w/hijack/{hid}/events?after_seq=notanumber"),
    )
    assert resp.status == 200
    data = _body(resp)
    assert data["after_seq"] == 0


# ---------------------------------------------------------------------------
# GET /hijack/{id}/events — 403 (non-admin) and 404 (no active session)
# ---------------------------------------------------------------------------


async def test_events_403_non_admin() -> None:
    """Line 221: non-admin role → 403 for /events."""
    # Acquire with admin, then check events as viewer
    runtime = _Runtime()
    r1 = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "a", "lease_s": 60}),
    )
    hid = _body(r1)["hijack_id"]
    runtime._role = "viewer"
    resp = await route_http(runtime, _Req(f"https://x/worker/w/hijack/{hid}/events"))
    assert resp.status == 403


async def test_events_404_no_active_session() -> None:
    """Line 227: valid hijack_id in path but no active session → 404."""
    runtime = _Runtime()
    # No acquire — hijack.session is None
    resp = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/aaaaaaaa-0000-0000-0000-000000000000/events"),
    )
    assert resp.status == 404


async def test_events_404_wrong_hijack_id() -> None:
    """Line 227: active session exists but path hijack_id doesn't match → 404."""
    runtime = _Runtime()
    await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "a", "lease_s": 60}),
    )
    resp = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/aaaaaaaa-0000-0000-0000-000000000000/events"),
    )
    assert resp.status == 404


# ---------------------------------------------------------------------------
# 404 fallthrough — line 264
# ---------------------------------------------------------------------------


async def test_unknown_path_returns_404() -> None:
    """Line 264: unrecognized path → 404 with error=not_found."""
    runtime = _Runtime()
    resp = await route_http(runtime, _Req("https://x/worker/w/unknown-endpoint"))
    assert resp.status == 404
    assert _body(resp)["error"] == "not_found"
