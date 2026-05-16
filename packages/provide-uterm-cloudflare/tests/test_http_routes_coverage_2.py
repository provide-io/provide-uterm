#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Coverage tests for api/http_routes.py — session endpoints, prompt guards, pagination."""

from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace

from provide.uterm.cloudflare.api.http_routes import route_http
from provide.uterm.cloudflare.bridge.hijack import HijackCoordinator, HijackSession


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
        self._subject: str | None = None
        self._browser_role = browser_role
        self.last_snapshot: dict | None = None
        self.last_analysis: str | None = None
        self.lifecycle_state = "stopped"
        self.input_mode: str = "hijack"
        self.browser_hijack_owner: dict[str, str] = {}

    async def request_json(self, request: object) -> dict:
        return json.loads(getattr(request, "_body", "{}"))

    async def browser_role_for_request(self, request: object) -> str:
        return self._role

    async def browser_subject_for_request(self, request: object) -> str | None:
        return self._subject

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
# GET /api/sessions/{id} — single session status
# ---------------------------------------------------------------------------


async def test_get_session_status_200() -> None:
    runtime = _Runtime()
    resp = await route_http(runtime, _Req("https://x/api/sessions/w"))
    assert resp.status == 200
    body = _body(resp)
    assert body["session_id"] == "w"
    assert body["input_mode"] == "hijack"
    assert body["connected"] is False


async def test_get_session_status_404_wrong_id() -> None:
    runtime = _Runtime()
    resp = await route_http(runtime, _Req("https://x/api/sessions/other"))
    assert resp.status == 404


# ---------------------------------------------------------------------------
# GET /api/sessions/{id}/snapshot
# ---------------------------------------------------------------------------


async def test_get_session_snapshot_none() -> None:
    runtime = _Runtime()
    resp = await route_http(runtime, _Req("https://x/api/sessions/w/snapshot"))
    assert resp.status == 200
    assert _body(resp)["snapshot"] is None


async def test_get_session_snapshot_with_data() -> None:
    runtime = _Runtime()
    runtime.last_snapshot = {"type": "snapshot", "screen": "hello", "ts": 1.0}
    resp = await route_http(runtime, _Req("https://x/api/sessions/w/snapshot"))
    assert resp.status == 200
    assert _body(resp)["snapshot"]["screen"] == "hello"


# ---------------------------------------------------------------------------
# GET /api/sessions/{id}/events
# ---------------------------------------------------------------------------


async def test_get_session_events_default() -> None:
    runtime = _Runtime()
    resp = await route_http(runtime, _Req("https://x/api/sessions/w/events"))
    assert resp.status == 200
    body = _body(resp)
    assert "events" in body
    assert "session_id" in body


async def test_get_session_events_limit_param() -> None:
    runtime = _Runtime()
    resp = await route_http(runtime, _Req("https://x/api/sessions/w/events?limit=50&after_seq=0"))
    assert resp.status == 200


# ---------------------------------------------------------------------------
# POST /api/sessions/{id}/mode
# ---------------------------------------------------------------------------


async def test_session_mode_set_open() -> None:
    runtime = _Runtime()
    resp = await route_http(
        runtime,
        _Req("https://x/api/sessions/w/mode", method="POST").with_body({"input_mode": "open"}),
    )
    assert resp.status == 200
    assert _body(resp)["input_mode"] == "open"


async def test_session_mode_invalid_value() -> None:
    runtime = _Runtime()
    resp = await route_http(
        runtime,
        _Req("https://x/api/sessions/w/mode", method="POST").with_body({"input_mode": "bad"}),
    )
    assert resp.status == 400


async def test_session_mode_non_admin_forbidden() -> None:
    runtime = _Runtime(role="viewer")
    resp = await route_http(
        runtime,
        _Req("https://x/api/sessions/w/mode", method="POST").with_body({"input_mode": "open"}),
    )
    assert resp.status == 403


# ---------------------------------------------------------------------------
# POST /api/sessions/{id}/clear
# ---------------------------------------------------------------------------


async def test_session_clear_no_worker() -> None:
    """Clear with no worker clears snapshot and returns status."""
    runtime = _Runtime(worker_ws=None)
    runtime.last_snapshot = {"type": "snapshot", "screen": "old"}
    resp = await route_http(runtime, _Req("https://x/api/sessions/w/clear", method="POST"))
    assert resp.status == 200
    assert runtime.last_snapshot is None


async def test_session_clear_owner_allowed() -> None:
    runtime = _Runtime(role="viewer")
    runtime.meta["visibility"] = "operator"
    runtime.meta["owner"] = "alice"
    runtime._subject = "alice"
    resp = await route_http(runtime, _Req("https://x/api/sessions/w/clear", method="POST"))
    assert resp.status == 200


async def test_session_clear_operator_non_owner_forbidden() -> None:
    runtime = _Runtime(role="operator")
    runtime.meta["visibility"] = "operator"
    runtime.meta["owner"] = "alice"
    runtime._subject = "bob"
    resp = await route_http(runtime, _Req("https://x/api/sessions/w/clear", method="POST"))
    assert resp.status == 403


# ---------------------------------------------------------------------------
# POST /api/sessions/{id}/analyze
# ---------------------------------------------------------------------------


async def test_session_analyze_no_worker() -> None:
    runtime = _Runtime(worker_ws=None)
    resp = await route_http(runtime, _Req("https://x/api/sessions/w/analyze", method="POST"))
    assert resp.status == 409


async def test_session_analyze_returns_cached() -> None:
    """Analyze with a worker returns cached last_analysis (timeout=0 fallback)."""
    _mock_ws = object()
    runtime = _Runtime(worker_ws=_mock_ws)
    runtime.last_analysis = "analysis result"
    resp = await route_http(runtime, _Req("https://x/api/sessions/w/analyze", method="POST"))
    assert resp.status == 200
    assert _body(resp)["analysis"] == "analysis result"


async def test_session_analyze_owner_allowed() -> None:
    _mock_ws = object()
    runtime = _Runtime(role="viewer", worker_ws=_mock_ws)
    runtime.meta["visibility"] = "operator"
    runtime.meta["owner"] = "alice"
    runtime._subject = "alice"
    runtime.last_analysis = "result"
    resp = await route_http(runtime, _Req("https://x/api/sessions/w/analyze", method="POST"))
    assert resp.status == 200


async def test_session_analyze_operator_non_owner_forbidden() -> None:
    runtime = _Runtime(role="operator")
    runtime.meta["visibility"] = "operator"
    runtime.meta["owner"] = "alice"
    runtime._subject = "bob"
    resp = await route_http(runtime, _Req("https://x/api/sessions/w/analyze", method="POST"))
    assert resp.status == 403


# ---------------------------------------------------------------------------
# GET /worker/{id}/hijack/{hid}/events — limit query param
# ---------------------------------------------------------------------------


async def test_hijack_events_limit_param() -> None:
    runtime = _Runtime()
    r1 = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "a", "lease_s": 60}),
    )
    hid = _body(r1)["hijack_id"]
    resp = await route_http(
        runtime,
        _Req(f"https://x/worker/w/hijack/{hid}/events?after_seq=0&limit=25"),
    )
    assert resp.status == 200


# ---------------------------------------------------------------------------
# Prompt guards in /send
# ---------------------------------------------------------------------------


async def test_send_with_expect_prompt_id_immediate_match() -> None:
    """expect_prompt_id matches immediately from last_snapshot."""
    _mock_ws = object()
    runtime = _Runtime(worker_ws=_mock_ws)
    runtime.last_snapshot = {
        "type": "snapshot",
        "screen": "$ ",
        "prompt_detected": {"prompt_id": "shell_prompt"},
    }
    r1 = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "a", "lease_s": 60}),
    )
    hid = _body(r1)["hijack_id"]
    resp = await route_http(
        runtime,
        _Req(f"https://x/worker/w/hijack/{hid}/send", method="POST").with_body(
            {"keys": "ls\r", "expect_prompt_id": "shell_prompt", "timeout_ms": 500}
        ),
    )
    assert resp.status == 200
    assert _body(resp)["matched_prompt_id"] == "shell_prompt"


async def test_send_with_expect_regex_immediate_match() -> None:
    """expect_regex matches against current screen."""
    _mock_ws = object()
    runtime = _Runtime(worker_ws=_mock_ws)
    runtime.last_snapshot = {"type": "snapshot", "screen": "user@host:~$ ", "ts": 1.0}
    r1 = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "a", "lease_s": 60}),
    )
    hid = _body(r1)["hijack_id"]
    resp = await route_http(
        runtime,
        _Req(f"https://x/worker/w/hijack/{hid}/send", method="POST").with_body(
            {"keys": "ls\r", "expect_regex": r"\$\s*$", "timeout_ms": 500}
        ),
    )
    assert resp.status == 200


async def test_send_without_guards_unchanged() -> None:
    """Send without prompt guards returns immediately as before."""
    _mock_ws = object()
    runtime = _Runtime(worker_ws=_mock_ws)
    r1 = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "a", "lease_s": 60}),
    )
    hid = _body(r1)["hijack_id"]
    resp = await route_http(
        runtime,
        _Req(f"https://x/worker/w/hijack/{hid}/send", method="POST").with_body({"keys": "ls\r"}),
    )
    assert resp.status == 200


# ---------------------------------------------------------------------------
# has_more pagination — uses >= so True when exactly limit rows returned
# ---------------------------------------------------------------------------


async def test_cf_hijack_events_has_more_true_when_exactly_limit() -> None:
    """has_more must be True when exactly limit events are returned (hijack events).

    Kills the mutation:
      len(rows) >= limit  →  len(rows) > limit
    """
    hid = str(uuid.uuid4())
    now = time.time()

    class _StoreWith5Events:
        def list_events_since(self, worker_id, after_seq, limit):
            # Return exactly 'limit' rows regardless of what limit is.
            return [{"seq": i + 1, "ts": now, "type": "snapshot"} for i in range(limit)]

        def current_event_seq(self, worker_id):
            return limit

        def min_event_seq(self, worker_id):
            return 1

        load_session = lambda *a, **k: None  # noqa: E731
        save_input_mode = lambda *a, **k: None  # noqa: E731
        append_event = lambda *a, **k: None  # noqa: E731

    limit = 5
    coord = HijackCoordinator()
    coord._session = HijackSession(hijack_id=hid, owner="test", lease_expires_at=now + 3600)

    class _RuntimeWith5:
        # Reuse _Runtime but override store and hijack
        worker_id = "w"
        meta: dict = {
            "display_name": "w",
            "connector_type": "unknown",
            "created_at": 0.0,
            "tags": [],
            "visibility": "public",
            "owner": None,
        }
        worker_ws = object()
        hijack = coord
        last_snapshot = None
        last_analysis = None
        lifecycle_state = "stopped"
        input_mode = "hijack"
        browser_hijack_owner: dict = {}
        _role = "admin"

        async def request_json(self, req):
            return {}

        async def browser_role_for_request(self, req):
            return "admin"

        def persist_lease(self, s):
            pass

        def clear_lease(self):
            pass

        async def push_worker_control(self, *a, **k):
            return True

        async def broadcast_hijack_state(self):
            pass

        async def push_worker_input(self, d):
            return True

        async def send_ws(self, ws, frame):
            pass

        def ws_key(self, ws):
            return str(id(ws))

        @property
        def store(self):
            return _StoreWith5Events()

    runtime = _RuntimeWith5()
    resp = await route_http(
        runtime,
        _Req(f"https://x/hijack/{hid}/events?limit={limit}&after_seq=0"),
    )
    body = json.loads(resp.body)
    assert body["has_more"] is True, "has_more must be True when exactly limit events returned"


async def test_cf_hijack_events_has_more_false_when_fewer() -> None:
    """has_more is False when fewer than limit events returned.

    Kills the mutation:
      len(rows) >= limit  →  True  (always)
    """
    hid = str(uuid.uuid4())
    now = time.time()

    class _StoreWith2:
        def list_events_since(self, worker_id, after_seq, limit):
            return [{"seq": 1, "ts": now, "type": "x"}, {"seq": 2, "ts": now, "type": "y"}]

        def current_event_seq(self, worker_id):
            return 2

        def min_event_seq(self, worker_id):
            return 1

        load_session = lambda *a, **k: None  # noqa: E731
        save_input_mode = lambda *a, **k: None  # noqa: E731
        append_event = lambda *a, **k: None  # noqa: E731

    coord = HijackCoordinator()
    coord._session = HijackSession(hijack_id=hid, owner="test", lease_expires_at=now + 3600)

    class _Runtime2:  # type: ignore[misc]
        worker_id = "w"
        meta: dict = {
            "display_name": "w",
            "connector_type": "unknown",
            "created_at": 0.0,
            "tags": [],
            "visibility": "public",
            "owner": None,
        }
        worker_ws = object()
        hijack = coord
        last_snapshot = None
        last_analysis = None
        lifecycle_state = "stopped"
        input_mode = "hijack"
        browser_hijack_owner: dict = {}

        async def request_json(self, req):
            return {}

        async def browser_role_for_request(self, req):
            return "admin"

        def persist_lease(self, s):
            pass

        def clear_lease(self):
            pass

        async def push_worker_control(self, *a, **k):
            return True

        async def broadcast_hijack_state(self):
            pass

        async def push_worker_input(self, d):
            return True

        async def send_ws(self, ws, frame):
            pass

        def ws_key(self, ws):
            return str(id(ws))

        @property
        def store(self):
            return _StoreWith2()

    runtime = _Runtime2()
    resp = await route_http(
        runtime,
        _Req(f"https://x/hijack/{hid}/events?limit=10&after_seq=0"),
    )
    body = json.loads(resp.body)
    assert body["has_more"] is False, "has_more must be False when fewer than limit events returned"
