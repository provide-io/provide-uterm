#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Additional coverage tests for api/http_routes.py — timeout/fallback branches."""

from __future__ import annotations

import json
from types import SimpleNamespace

from provide_terminal_cloudflare.api.http_routes import (
    _wait_for_analysis,
    _wait_for_prompt,
    route_http,
)
from provide_terminal_cloudflare.bridge.hijack import HijackCoordinator


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
    ) -> None:
        self.worker_id = "w"
        self.worker_ws = worker_ws
        self.hijack = HijackCoordinator()
        self._role = role
        self.last_snapshot: dict | None = None
        self.last_analysis: str | None = None
        self.input_mode: str = "hijack"
        self.browser_hijack_owner: dict[str, str] = {}
        self._sent: list[dict] = []

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
        self._sent.append(frame)

    def ws_key(self, ws: object) -> str:
        return str(id(ws))

    def _socket_browser_role(self, ws: object) -> str:
        return "admin"

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
# _wait_for_prompt — timeout path (lines 75-76)
# ---------------------------------------------------------------------------


async def test_wait_for_prompt_timeout_returns_last_snapshot() -> None:
    """_wait_for_prompt returns last_snapshot when no match within timeout."""
    runtime = _Runtime()
    runtime.last_snapshot = {"type": "snapshot", "screen": "other"}
    result = await _wait_for_prompt(
        runtime,
        expect_prompt_id="shell_prompt",
        expect_regex=None,
        timeout_ms=100,
        poll_interval_ms=50,
    )
    # Returns last_snapshot even on timeout (no match)
    assert result == runtime.last_snapshot


# ---------------------------------------------------------------------------
# _wait_for_analysis — timeout path (lines 86-87)
# ---------------------------------------------------------------------------


async def test_wait_for_analysis_timeout_returns_none() -> None:
    """_wait_for_analysis returns None when last_analysis stays None within timeout."""
    runtime = _Runtime()
    result = await _wait_for_analysis(runtime, timeout_ms=100)
    assert result is None


# ---------------------------------------------------------------------------
# /send — bad timeout_ms/poll_interval_ms triggers except branch (lines 236-237)
# ---------------------------------------------------------------------------


async def test_send_bad_timeout_ms_uses_defaults() -> None:
    """send with non-numeric timeout_ms falls back to defaults (TypeError)."""
    mock_ws = object()
    runtime = _Runtime(worker_ws=mock_ws)
    runtime.last_snapshot = {
        "type": "snapshot",
        "screen": "$ ",
        "prompt_detected": {"prompt_id": "shell"},
    }
    r1 = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "a", "lease_s": 60}),
    )
    hid = _body(r1)["hijack_id"]
    resp = await route_http(
        runtime,
        _Req(f"https://x/worker/w/hijack/{hid}/send", method="POST").with_body(
            {"keys": "ls\r", "expect_prompt_id": "shell", "timeout_ms": "bad", "poll_interval_ms": None}
        ),
    )
    assert resp.status == 200


# ---------------------------------------------------------------------------
# /hijack/{hid}/events — bad limit param falls back to 100 (lines 296-297)
# ---------------------------------------------------------------------------


async def test_hijack_events_bad_limit_uses_default() -> None:
    """events with non-numeric limit falls back to default 100."""
    runtime = _Runtime()
    r1 = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "a", "lease_s": 60}),
    )
    hid = _body(r1)["hijack_id"]
    resp = await route_http(
        runtime,
        _Req(f"https://x/worker/w/hijack/{hid}/events?limit=notanumber"),
    )
    assert resp.status == 200


# ---------------------------------------------------------------------------
# /api/sessions/{id}/events — bad after_seq/limit (lines 367-372)
# ---------------------------------------------------------------------------


async def test_session_events_bad_after_seq_uses_default() -> None:
    """events with non-numeric after_seq falls back to 0."""
    runtime = _Runtime()
    resp = await route_http(runtime, _Req("https://x/api/sessions/w/events?after_seq=notanumber"))
    assert resp.status == 200


async def test_session_events_bad_limit_uses_default() -> None:
    """events with non-numeric limit falls back to 100."""
    runtime = _Runtime()
    resp = await route_http(runtime, _Req("https://x/api/sessions/w/events?limit=notanumber"))
    assert resp.status == 200


# ---------------------------------------------------------------------------
# /api/sessions/{id}/mode — blocked when hijack active (line 395)
# ---------------------------------------------------------------------------


async def test_session_mode_blocked_when_hijack_active() -> None:
    """POST /api/sessions/{id}/mode with mode=open while hijack is active returns 409."""
    runtime = _Runtime()
    runtime.hijack.acquire("alice", lease_s=60)
    resp = await route_http(
        runtime,
        _Req("https://x/api/sessions/w/mode", method="POST").with_body({"input_mode": "open"}),
    )
    assert resp.status == 409


# ---------------------------------------------------------------------------
# /api/sessions/{id}/clear — with worker connected sends snapshot_req (line 406)
# ---------------------------------------------------------------------------


async def test_session_clear_with_worker_sends_snapshot_req() -> None:
    """POST /api/sessions/{id}/clear with live worker sends snapshot_req frame."""
    mock_ws = object()
    runtime = _Runtime(worker_ws=mock_ws)
    resp = await route_http(
        runtime,
        _Req("https://x/api/sessions/w/clear", method="POST"),
    )
    assert resp.status == 200
    assert runtime.last_snapshot is None
    assert any(f.get("type") == "snapshot_req" for f in runtime._sent)


# ---------------------------------------------------------------------------
# /api/sessions/{id}/<unknown sub+method> — fallthrough 404 (line 419)
# ---------------------------------------------------------------------------


async def test_session_unknown_sub_method_returns_404() -> None:
    """Unknown sub/method combo under session_match returns 404."""
    runtime = _Runtime()
    resp = await route_http(
        runtime,
        _Req("https://x/api/sessions/w/snapshot", method="POST"),  # snapshot only allows GET
    )
    assert resp.status == 404


# ---------------------------------------------------------------------------
# /send — invalid expect_regex returns 400 (ReDoS guard)
# ---------------------------------------------------------------------------


async def test_send_invalid_expect_regex_returns_400() -> None:
    """send with an invalid expect_regex pattern returns 400 (not a 500)."""
    mock_ws = object()
    runtime = _Runtime(worker_ws=mock_ws)
    r1 = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "a", "lease_s": 60}),
    )
    hid = _body(r1)["hijack_id"]
    resp = await route_http(
        runtime,
        _Req(f"https://x/worker/w/hijack/{hid}/send", method="POST").with_body(
            {"keys": "ls\r", "expect_regex": "[invalid("}
        ),
    )
    assert resp.status == 400
    assert "invalid expect_regex" in _body(resp).get("error", "")


async def test_send_expect_regex_too_long_returns_400() -> None:
    """send with an expect_regex exceeding _MAX_REGEX_LEN returns 400."""
    from provide_terminal_cloudflare.api.http_routes import _MAX_REGEX_LEN

    mock_ws = object()
    runtime = _Runtime(worker_ws=mock_ws)
    r1 = await route_http(
        runtime,
        _Req("https://x/worker/w/hijack/acquire", method="POST").with_body({"owner": "a", "lease_s": 60}),
    )
    hid = _body(r1)["hijack_id"]
    resp = await route_http(
        runtime,
        _Req(f"https://x/worker/w/hijack/{hid}/send", method="POST").with_body(
            {"keys": "ls\r", "expect_regex": "a" * (_MAX_REGEX_LEN + 1)}
        ),
    )
    assert resp.status == 400
    assert "expect_regex too long" in _body(resp).get("error", "")
