#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Hijack ownership fencing over REST/browser channels, expect-regex guards, and tunnel invites."""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cf_fencing_helpers import (
    _BlockingWorkerWs,
    _BrowserWs,
    _control,
    _Request,
    _runtime,
    _send,
)
from provide.uterm.cloudflare.api._tunnel_api import _clear_tunnel_invite, consume_tunnel_invite
from provide.uterm.cloudflare.api.http_routes._hijack import route_hijack
from provide.uterm.cloudflare.api.http_routes._shared import _looks_like_counted_quantifier, compile_expect_regex
from provide.uterm.cloudflare.api.ws_routes import handle_socket_message
from provide.uterm.cloudflare.contracts import frame_json

from provide.uterm.tunnel.token_hash import hash_token


async def test_rest_heartbeat_uses_authenticated_acquirer_not_display_owner() -> None:
    runtime = _runtime()
    worker = _BlockingWorkerWs()
    worker.release.set()
    runtime.worker_ws = worker
    subject = "authenticated-subject"

    async def resolve(_request: object) -> tuple[object, None]:
        return SimpleNamespace(subject_id=subject), None

    runtime.resolve_principal = resolve  # type: ignore[method-assign]
    acquired_response = await route_hijack(
        runtime,
        _Request({"owner": "display-label", "lease_s": 30}),
        f"/worker/{runtime.worker_id}/hijack/acquire",
        "https://example.invalid/acquire",
        "POST",
    )
    assert getattr(acquired_response, "status", None) == 200
    active = runtime.hijack.session
    assert active is not None
    assert active.owner == "display-label"
    assert active.acquired_by == subject

    heartbeat = await route_hijack(
        runtime,
        _Request({"lease_s": 45}),
        f"/worker/{runtime.worker_id}/hijack/{active.hijack_id}/heartbeat",
        "https://example.invalid/heartbeat",
        "POST",
    )
    assert getattr(heartbeat, "status", None) == 200

    async def resolve_competitor(_request: object) -> tuple[object, None]:
        return SimpleNamespace(subject_id="different-subject"), None

    runtime.resolve_principal = resolve_competitor  # type: ignore[method-assign]
    refused = await route_hijack(
        runtime,
        _Request({"lease_s": 45}),
        f"/worker/{runtime.worker_id}/hijack/{active.hijack_id}/heartbeat",
        "https://example.invalid/heartbeat",
        "POST",
    )
    assert getattr(refused, "status", None) == 409


async def test_invalid_expect_regex_sends_zero_worker_frames() -> None:
    runtime = _runtime()
    active = runtime.hijack.acquire("owner", 60)
    assert active.session is not None
    worker = _BlockingWorkerWs()
    worker.release.set()
    runtime.worker_ws = worker

    response = await _send(runtime, active.session.hijack_id, {"keys": "must-not-send", "expect_regex": "["})

    assert getattr(response, "status", None) == 400
    assert worker.sent == []


@pytest.mark.parametrize(
    "unsafe_pattern",
    [
        "a{,}",
        "a{,3}",
        "a*a*a*a*a*a*a*a*b",
        "a?a?a?a?a?a?a?a?b",
        "a|b",
        "(?=a)",
        "(?!a)",
        "(?<=a)",
        "(?<!a)",
        r"(a)\1",
        r"(?P<letter>a)(?P=letter)",
    ],
)
async def test_unsafe_expect_regex_sends_zero_worker_frames(unsafe_pattern: str) -> None:
    runtime = _runtime()
    active = runtime.hijack.acquire("owner", 60)
    assert active.session is not None
    worker = _BlockingWorkerWs()
    worker.release.set()
    runtime.worker_ws = worker

    response = await _send(runtime, active.session.hijack_id, {"keys": "must-not-send", "expect_regex": unsafe_pattern})

    assert getattr(response, "status", None) == 400
    assert worker.sent == []


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("a{", False),
        ("a{}", False),
        ("a{2}", True),
        ("a{x}", False),
        ("a{,}", True),
        ("a{,3}", True),
        ("a{2,}", True),
        ("a{2,3}", True),
        ("a{x,3}", False),
        ("a{2,x}", False),
    ],
)
def test_counted_quantifier_parser_matches_python_forms(pattern: str, expected: bool) -> None:
    assert _looks_like_counted_quantifier(pattern, 1) is expected


@pytest.mark.parametrize("pattern", ["a{", "a{}", "a{x}", "a{2,x}", "a{2}", "a{2,}", "a{2,3}", "[?]"])
def test_conservative_regex_grammar_preserves_safe_patterns(pattern: str) -> None:
    assert compile_expect_regex(pattern) is not None


def test_empty_expect_regex_disables_the_guard() -> None:
    assert compile_expect_regex(None) is None


async def test_browser_removal_covers_missing_token_and_failed_release(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    browser = _BrowserWs()
    active = runtime.hijack.acquire("owner", 60).session
    assert active is not None
    browser_id = runtime.ws_key(browser)
    runtime.browser_hijack_owner[browser_id] = active.hijack_id
    assert await runtime.remove_browser_socket(browser)

    active = runtime.hijack.acquire("owner", 60).session
    assert active is not None
    runtime.browser_hijack_owner[browser_id] = active.hijack_id
    monkeypatch.setattr(runtime.hijack, "release", lambda _hijack_id: SimpleNamespace(ok=False))
    assert not await runtime.remove_browser_socket(browser)


async def test_tunnel_invite_redemption_fail_closed_boundaries() -> None:
    runtime = _runtime()

    class BrokenRequest:
        async def text(self) -> str:
            raise RuntimeError("broken body")

    assert (await runtime._redeem_tunnel_invite(BrokenRequest())).status == 404
    assert (await runtime._redeem_tunnel_invite(_Request({"invite": "invite"}))).status == 404

    class BrokenKv:
        async def get(self, _key: str) -> str:
            raise RuntimeError("broken kv")

    runtime.env.SESSION_REGISTRY = BrokenKv()
    assert (await runtime._redeem_tunnel_invite(_Request({"invite": "invite"}))).status == 404

    class Kv:
        def __init__(self, value: dict[str, object]) -> None:
            self.value = value

        async def get(self, _key: str) -> str:
            return json.dumps(self.value)

    runtime.env.SESSION_REGISTRY = Kv({"revoked": True})
    assert (await runtime._redeem_tunnel_invite(_Request({"invite": "invite"}))).status == 404
    runtime.env.SESSION_REGISTRY = Kv({"expires_at": 0})
    assert (await runtime._redeem_tunnel_invite(_Request({"invite": "invite"}))).status == 404
    runtime.env.SESSION_REGISTRY = Kv(
        {
            "control_token_hash": "active",
            "control_invite_hash": "invite",
            "control_invite_token": "token",
            "control_invite_expires_at": 0,
            "share_token_hash": "active",
            "share_invite_hash": "invite",
            "share_invite_token": "token",
            "share_invite_expires_at": 0,
        }
    )
    assert (await runtime._redeem_tunnel_invite(_Request({"invite": "invite"}))).status == 404


def test_lazy_worker_identity_rejects_malformed_internal_paths() -> None:
    runtime = _runtime()
    runtime.worker_id = "default"
    runtime._lazy_init_worker_id(SimpleNamespace(url="https://example.invalid/_internal/tunnel-invite/id/not-redeem"))
    runtime._lazy_init_worker_id(SimpleNamespace(url="https://example.invalid/_internal/tunnel-invite/%2F/redeem"))
    assert runtime.worker_id == "default"


async def test_fetch_treats_broken_headers_as_untrusted() -> None:
    runtime = _runtime()

    class Headers:
        def get(self, _key: str) -> str:
            raise RuntimeError("broken headers")

    request = SimpleNamespace(
        url="https://example.invalid/_internal/tunnel-invite/fence-worker/redeem",
        method="POST",
        headers=Headers(),
    )
    response = await runtime._fetch_impl(request)
    assert response.status == 404


async def test_rest_acquire_rejects_identity_change_and_competing_owner() -> None:
    runtime = _runtime()
    worker = _BlockingWorkerWs()
    worker.release.set()
    runtime.worker_ws = worker
    runtime.resolve_principal = AsyncMock(return_value=(SimpleNamespace(subject_id="first"), None))
    first = await route_hijack(
        runtime,
        _Request({"owner": "same"}),
        f"/worker/{runtime.worker_id}/hijack/acquire",
        "https://example.invalid/acquire",
        "POST",
    )
    assert getattr(first, "status", None) == 200

    runtime.resolve_principal = AsyncMock(return_value=(SimpleNamespace(subject_id="second"), None))
    mismatch = await route_hijack(
        runtime,
        _Request({"owner": "same"}),
        f"/worker/{runtime.worker_id}/hijack/acquire",
        "https://example.invalid/acquire",
        "POST",
    )
    assert getattr(mismatch, "status", None) == 409

    runtime.resolve_principal = AsyncMock(return_value=(SimpleNamespace(subject_id="first"), None))
    busy = await route_hijack(
        runtime,
        _Request({"owner": "different"}),
        f"/worker/{runtime.worker_id}/hijack/acquire",
        "https://example.invalid/acquire",
        "POST",
    )
    assert getattr(busy, "status", None) == 409


async def test_browser_hijack_request_refusal_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    runtime.worker_ws = _BlockingWorkerWs()

    viewer = _BrowserWs("viewer")
    await handle_socket_message(runtime, viewer, frame_json("hijack_request"), is_worker=False)
    assert any(_control(frame).get("message") == "hijack_requires_admin" for frame in viewer.sent)

    runtime.input_mode = "open"
    open_mode = _BrowserWs()
    await handle_socket_message(runtime, open_mode, frame_json("hijack_request"), is_worker=False)
    assert any(_control(frame).get("message") == "hijack_unavailable_in_open_mode" for frame in open_mode.sent)

    runtime.input_mode = "hijack"
    assert runtime.hijack.acquire("another", 60).session is not None
    busy = _BrowserWs()
    await handle_socket_message(runtime, busy, frame_json("hijack_request"), is_worker=False)
    assert any(_control(frame).get("message") == "already_hijacked" for frame in busy.sent)

    runtime.hijack._session = None
    monkeypatch.setattr(runtime, "push_worker_control", AsyncMock(return_value=False))
    no_pause = _BrowserWs()
    await handle_socket_message(runtime, no_pause, frame_json("hijack_request"), is_worker=False)
    assert runtime.hijack.session is None
    assert any(_control(frame).get("message") == "no_worker" for frame in no_pause.sent)


async def test_browser_hijack_step_and_release_failure_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    runtime.worker_ws = _BlockingWorkerWs()
    owner = _BrowserWs()
    active = runtime.hijack.acquire("owner", 60).session
    assert active is not None
    runtime.browser_hijack_owner[runtime.ws_key(owner)] = active.hijack_id

    monkeypatch.setattr(runtime, "push_worker_control", AsyncMock(return_value=False))
    await handle_socket_message(runtime, owner, frame_json("hijack_step"), is_worker=False)
    assert any(_control(frame).get("message") == "no_worker" for frame in owner.sent)

    monkeypatch.setattr(runtime.hijack, "release", lambda _hijack_id: SimpleNamespace(ok=False, error="release_failed"))
    await handle_socket_message(runtime, owner, frame_json("hijack_release"), is_worker=False)
    assert any(_control(frame).get("message") == "release_failed" for frame in owner.sent)


async def test_browser_hijack_release_reports_failed_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    runtime.worker_ws = _BlockingWorkerWs()
    owner = _BrowserWs()
    active = runtime.hijack.acquire("owner", 60).session
    assert active is not None
    runtime.browser_hijack_owner[runtime.ws_key(owner)] = active.hijack_id
    monkeypatch.setattr(runtime, "push_worker_control", AsyncMock(return_value=False))

    await handle_socket_message(runtime, owner, frame_json("hijack_release"), is_worker=False)

    assert any(_control(frame).get("message") == "no_worker" for frame in owner.sent)


async def test_resume_rejects_owner_when_pause_cannot_be_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    runtime.worker_ws = _BlockingWorkerWs()
    runtime.store.create_resume_token("old-owner", runtime.worker_id, "admin", 60)
    runtime.store.mark_resume_hijack_owner("old-owner", True)
    monkeypatch.setattr(runtime, "push_worker_control", AsyncMock(return_value=False))
    resumed = _BrowserWs()

    await handle_socket_message(runtime, resumed, frame_json("resume", token="old-owner"), is_worker=False)

    assert runtime.hijack.session is None
    assert resumed.sent == []


async def test_resume_rejects_lease_that_expires_while_worker_pause_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    monkeypatch.setattr(runtime.config, "hijack_lease_s", 1)
    runtime.worker_ws = _BlockingWorkerWs()
    runtime.store.create_resume_token("expiring-owner", runtime.worker_id, "admin", 60)
    runtime.store.mark_resume_hijack_owner("expiring-owner", True)
    actions: list[str] = []

    async def delayed_control(action: str, *, owner: str, lease_s: int) -> bool:
        _ = (owner, lease_s)
        actions.append(action)
        if action == "pause":
            await asyncio.sleep(1.05)
        return True

    monkeypatch.setattr(runtime, "push_worker_control", delayed_control)
    resumed = _BrowserWs()

    await handle_socket_message(runtime, resumed, frame_json("resume", token="expiring-owner"), is_worker=False)

    socket_id = runtime.ws_key(resumed)
    assert actions == ["pause", "resume"]
    assert runtime.hijack.session is None
    assert socket_id not in runtime.browser_hijack_owner
    assert socket_id not in runtime.browser_resume_tokens
    assert resumed.sent == []
    persisted = runtime.store.load_session(runtime.worker_id)
    assert persisted is not None and persisted["hijack_id"] is None


def test_clear_single_tunnel_invite_uses_role_prefix() -> None:
    entry = {
        "control_invite_hash": "control",
        "control_invite_token": "control",
        "control_invite_expires_at": 1,
        "share_invite_hash": "share",
        "share_invite_token": "share",
        "share_invite_expires_at": 1,
    }
    _clear_tunnel_invite(entry, "operator")
    assert "control_invite_hash" not in entry and entry["share_invite_hash"] == "share"
    _clear_tunnel_invite(entry, "viewer")
    assert entry == {}


async def test_tunnel_invite_proxy_handles_js_and_fail_closed_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    request = SimpleNamespace(url="https://example.invalid/s/tid?invite=one")

    class Stub:
        def __init__(self, response: object) -> None:
            self.response = response

        async def fetch(self, _request: object) -> object:
            return self.response

    class Namespace:
        def __init__(self, response: object) -> None:
            self.response = response

        def idFromName(self, value: str) -> str:  # noqa: N802
            return value

        def get(self, _value: str) -> Stub:
            return Stub(self.response)

    js = ModuleType("js")
    js.Request = lambda url, init: SimpleNamespace(url=url, init=init)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "js", js)
    response = SimpleNamespace(
        status=200,
        text=AsyncMock(return_value=json.dumps({"page": "session", "role": "viewer", "token": "token"})),
    )
    assert await consume_tunnel_invite(request, SimpleNamespace(SESSION_RUNTIME=Namespace(response)), "tid") == (
        "session",
        "viewer",
        "token",
    )

    non_ok = SimpleNamespace(status=409, body="")
    assert await consume_tunnel_invite(request, SimpleNamespace(SESSION_RUNTIME=Namespace(non_ok)), "tid") is None
    invalid = SimpleNamespace(status=200, body=json.dumps({"page": 1, "role": "viewer", "token": "token"}))
    assert await consume_tunnel_invite(request, SimpleNamespace(SESSION_RUNTIME=Namespace(invalid)), "tid") is None

    class BrokenNamespace(Namespace):
        def get(self, _value: str) -> Stub:
            raise RuntimeError("binding failed")

    assert (
        await consume_tunnel_invite(
            request,
            SimpleNamespace(SESSION_RUNTIME=BrokenNamespace(response)),
            "tid",
        )
        is None
    )


async def test_tunnel_invite_mismatch_checks_both_roles() -> None:
    runtime = _runtime()

    class Kv:
        async def get(self, _key: str) -> str:
            return json.dumps(
                {
                    "control_token_hash": hash_token("control-token"),
                    "control_invite_hash": hash_token("control-invite"),
                    "control_invite_token": "control-token",
                    "share_token_hash": hash_token("share-token"),
                    "share_invite_hash": hash_token("share-invite"),
                    "share_invite_token": "share-token",
                }
            )

    runtime.env.SESSION_REGISTRY = Kv()
    response = await runtime._redeem_tunnel_invite(_Request({"invite": "not-either-invite"}))
    assert response.status == 404


async def test_browser_hijack_control_is_public_and_owner_fenced() -> None:
    runtime = _runtime()
    worker = _BlockingWorkerWs()
    worker.release.set()
    runtime.worker_ws = worker
    owner = _BrowserWs()
    competitor = _BrowserWs()

    await handle_socket_message(runtime, owner, frame_json("hijack_request"), is_worker=False)
    active = runtime.hijack.session
    assert active is not None
    assert runtime.browser_hijack_owner[runtime.ws_key(owner)] == active.hijack_id
    assert _control(worker.sent[0])["action"] == "pause"

    before = len(worker.sent)
    await handle_socket_message(runtime, competitor, frame_json("hijack_step"), is_worker=False)
    assert len(worker.sent) == before
    assert any(_control(raw).get("message") == "not_owner" for raw in competitor.sent)

    await handle_socket_message(runtime, owner, frame_json("hijack_step"), is_worker=False)
    assert _control(worker.sent[-1])["action"] == "step"

    await handle_socket_message(runtime, owner, frame_json("hijack_release"), is_worker=False)
    assert runtime.hijack.session is None
    assert _control(worker.sent[-1])["action"] == "resume"


async def test_browser_owner_disconnect_can_resume_before_competitor() -> None:
    runtime = _runtime()
    worker = _BlockingWorkerWs()
    worker.release.set()
    runtime.worker_ws = worker
    owner = _BrowserWs()
    runtime.browser_sockets[runtime.ws_key(owner)] = owner
    runtime.store.create_resume_token("owner-token", runtime.worker_id, "admin", 60)
    runtime.browser_resume_tokens[runtime.ws_key(owner)] = "owner-token"
    await handle_socket_message(runtime, owner, frame_json("hijack_request"), is_worker=False)

    await runtime.webSocketClose(owner, 1000, "gone")
    assert runtime.hijack.session is None
    record = runtime.store.get_resume_token("owner-token")
    assert record is not None and record["was_hijack_owner"] is True

    resumed = _BrowserWs()
    await handle_socket_message(runtime, resumed, frame_json("resume", token="owner-token"), is_worker=False)

    assert runtime.hijack.session is not None
    assert runtime.browser_hijack_owner[runtime.ws_key(resumed)] == runtime.hijack.session.hijack_id
    assert any(_control(raw).get("resumed") is True for raw in resumed.sent)


async def test_stale_resume_cannot_steal_from_competing_browser() -> None:
    runtime = _runtime()
    worker = _BlockingWorkerWs()
    worker.release.set()
    runtime.worker_ws = worker
    owner = _BrowserWs()
    runtime.browser_sockets[runtime.ws_key(owner)] = owner
    runtime.store.create_resume_token("stale-token", runtime.worker_id, "admin", 60)
    runtime.browser_resume_tokens[runtime.ws_key(owner)] = "stale-token"
    await handle_socket_message(runtime, owner, frame_json("hijack_request"), is_worker=False)
    await runtime.webSocketClose(owner, 1000, "gone")

    competitor = _BrowserWs()
    runtime.store.create_resume_token("competitor-token", runtime.worker_id, "admin", 60)
    runtime.browser_resume_tokens[runtime.ws_key(competitor)] = "competitor-token"
    await handle_socket_message(runtime, competitor, frame_json("hijack_request"), is_worker=False)
    competing_session = runtime.hijack.session
    assert competing_session is not None

    stale = _BrowserWs()
    await handle_socket_message(runtime, stale, frame_json("resume", token="stale-token"), is_worker=False)

    assert runtime.hijack.session is competing_session
    assert runtime.browser_hijack_owner[runtime.ws_key(competitor)] == competing_session.hijack_id
    assert not any(_control(raw).get("resumed") is True for raw in stale.sent)
