#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage tests for api/http_routes — DELETE session and restart routes."""

from __future__ import annotations

import json
from types import SimpleNamespace

from provide.terminal.cloudflare.api.http_routes import route_http
from provide.terminal.cloudflare.bridge.hijack import HijackCoordinator


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
        worker_id: str = "w",
    ) -> None:
        self.worker_id = worker_id
        self.worker_ws = worker_ws
        self.hijack = HijackCoordinator()
        self._role = role
        self.last_snapshot: dict | None = None
        self.last_analysis: str | None = None
        self.input_mode: str = "hijack"

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
# DELETE /api/sessions/{id} — session teardown
# ---------------------------------------------------------------------------


async def test_session_delete_no_worker() -> None:
    """DELETE /api/sessions/{id} with no worker connected returns ok."""
    runtime = _Runtime(role="admin")
    resp = await route_http(runtime, _Req("https://x/api/sessions/w", method="DELETE"))
    assert resp.status == 200
    body = _body(resp)
    assert body["deleted"] is True


async def test_session_delete_with_worker() -> None:
    """DELETE /api/sessions/{id} closes the worker WebSocket if connected."""

    class _FakeWS:
        closed_with: tuple | None = None

        def close(self, code: int, reason: str) -> None:
            self.closed_with = (code, reason)

    ws = _FakeWS()
    runtime = _Runtime(role="admin", worker_ws=ws)
    resp = await route_http(runtime, _Req("https://x/api/sessions/w", method="DELETE"))
    assert resp.status == 200
    assert ws.closed_with == (1001, "session deleted")


async def test_session_delete_viewer_forbidden() -> None:
    """DELETE /api/sessions/{id} requires operator or admin role."""
    runtime = _Runtime(role="viewer")
    resp = await route_http(runtime, _Req("https://x/api/sessions/w", method="DELETE"))
    assert resp.status == 403


# ---------------------------------------------------------------------------
# POST /api/sessions/{id}/restart — restart worker
# ---------------------------------------------------------------------------


async def test_session_restart_no_worker() -> None:
    """POST /api/sessions/{id}/restart with no worker returns restarted=True."""
    runtime = _Runtime(role="admin")
    resp = await route_http(runtime, _Req("https://x/api/sessions/w/restart", method="POST"))
    assert resp.status == 200
    body = _body(resp)
    assert body["restarted"] is True


async def test_session_restart_with_worker() -> None:
    """POST /api/sessions/{id}/restart closes the worker WebSocket."""

    class _FakeWS:
        closed_with: tuple | None = None

        def close(self, code: int, reason: str) -> None:
            self.closed_with = (code, reason)

    ws = _FakeWS()
    runtime = _Runtime(role="admin", worker_ws=ws)
    resp = await route_http(runtime, _Req("https://x/api/sessions/w/restart", method="POST"))
    assert resp.status == 200
    assert ws.closed_with == (1001, "restart requested")


async def test_session_restart_viewer_forbidden() -> None:
    """POST /api/sessions/{id}/restart requires operator or admin role."""
    runtime = _Runtime(role="viewer")
    resp = await route_http(runtime, _Req("https://x/api/sessions/w/restart", method="POST"))
    assert resp.status == 403
