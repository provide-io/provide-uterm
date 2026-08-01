#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Deterministic ownership-fencing races for the Cloudflare Durable Object."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from provide.uterm.cloudflare.api._tunnel_api import _clear_tunnel_invite, consume_tunnel_invite
from provide.uterm.cloudflare.api.http_routes import route_http
from provide.uterm.cloudflare.api.http_routes._hijack import route_hijack
from provide.uterm.cloudflare.api.http_routes._shared import _looks_like_counted_quantifier, compile_expect_regex
from provide.uterm.cloudflare.api.ws_routes import handle_socket_message
from provide.uterm.cloudflare.contracts import frame_json
from provide.uterm.cloudflare.do.session_runtime import SessionRuntime
from provide.uterm.cloudflare.do.ushell import _recording_available

from provide.uterm.control_channel import ControlChunk, ControlFrameDecoder
from provide.uterm.tunnel.token_hash import hash_token

_KEY = "test-secret-key-32-bytes-minimum!"


class _Request:
    def __init__(
        self,
        body: dict[str, object] | None = None,
        *,
        url: str = "https://example.invalid/",
        method: str = "POST",
    ) -> None:
        self.headers = {"Content-Type": "application/json"}
        self._body = json.dumps(body or {})
        self.url = url
        self.method = method

    async def text(self) -> str:
        return self._body


class _BrowserWs:
    def __init__(self, role: str = "admin") -> None:
        self.role = role
        self.sent: list[str] = []

    def deserializeAttachment(self) -> str:  # noqa: N802 - Cloudflare WebSocket API
        return f"browser:{self.role}:fence-worker"

    def serializeAttachment(self, attachment: str) -> None:  # noqa: N802 - Cloudflare WebSocket API
        self.attachment = attachment

    async def send(self, data: str) -> None:
        self.sent.append(data)


class _BlockingWorkerWs:
    """Hold the first worker send so a lifecycle transition can race it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.sent: list[str] = []
        self._blocked = False
        self.attachment = "worker:admin:fence-worker"
        self.closed: tuple[int, str] | None = None

    def deserializeAttachment(self) -> str:  # noqa: N802 - Cloudflare WebSocket API
        return self.attachment

    def serializeAttachment(self, attachment: str) -> None:  # noqa: N802 - Cloudflare WebSocket API
        self.attachment = attachment

    def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)

    async def send(self, data: str) -> None:
        self.sent.append(data)
        if not self._blocked:
            self._blocked = True
            self.started.set()
            await self.release.wait()


class _FailingBrowserWs(_BrowserWs):
    async def send(self, data: str) -> None:
        raise RuntimeError("browser disconnected")


def _runtime() -> SessionRuntime:
    conn = sqlite3.connect(":memory:")
    ctx = SimpleNamespace(
        storage=SimpleNamespace(sql=SimpleNamespace(exec=conn.execute), setAlarm=lambda _ms: None),
        id=SimpleNamespace(name=lambda: "fence-worker"),
        getWebSockets=list,
    )
    env = SimpleNamespace(
        AUTH_MODE="jwt",
        JWT_ALGORITHMS="HS256",
        JWT_PUBLIC_KEY_PEM=_KEY,
        WORKER_BEARER_TOKEN="test-worker-token-padded-to-32xyz",
    )
    runtime = SessionRuntime(ctx, env)

    async def admin_role(_request: object) -> str:
        return "admin"

    runtime.browser_role_for_request = admin_role  # type: ignore[method-assign]
    return runtime


def _control(raw: str) -> dict[str, object]:
    chunks = ControlFrameDecoder().feed(raw)
    assert len(chunks) == 1 and isinstance(chunks[0], ControlChunk)
    return chunks[0].control


async def _release(runtime: SessionRuntime, hijack_id: str) -> object:
    return await route_hijack(
        runtime,
        _Request(),
        f"/worker/{runtime.worker_id}/hijack/{hijack_id}/release",
        "https://example.invalid/release",
        "POST",
    )


@pytest.mark.parametrize("source", ["rest_send", "rest_step", "browser"])
async def test_release_waits_for_owned_delivery(source: str) -> None:
    runtime = _runtime()
    acquired = runtime.hijack.acquire("owner", 60)
    assert acquired.session is not None
    hijack_id = acquired.session.hijack_id
    worker = _BlockingWorkerWs()
    runtime.worker_ws = worker

    if source == "rest_send":
        delivery = route_hijack(
            runtime,
            _Request({"keys": "owned-input"}),
            f"/worker/{runtime.worker_id}/hijack/{hijack_id}/send",
            "https://example.invalid/send",
            "POST",
        )
    elif source == "rest_step":
        delivery = route_hijack(
            runtime,
            _Request(),
            f"/worker/{runtime.worker_id}/hijack/{hijack_id}/step",
            "https://example.invalid/step",
            "POST",
        )
    else:
        browser = _BrowserWs()
        runtime.browser_hijack_owner[runtime.ws_key(browser)] = hijack_id
        delivery = handle_socket_message(runtime, browser, frame_json("input", data="owned-input"), is_worker=False)

    delivery_task = asyncio.create_task(delivery)
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    release_task = asyncio.create_task(_release(runtime, hijack_id))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not release_task.done(), "release crossed an in-flight owned delivery"
    assert runtime.hijack.session is not None

    worker.release.set()
    await asyncio.wait_for(delivery_task, timeout=1)
    response = await asyncio.wait_for(release_task, timeout=1)
    assert getattr(response, "status", None) == 200
    assert runtime.hijack.session is None
    assert len(worker.sent) == 2


async def test_alarm_expiry_waits_for_owned_delivery() -> None:
    runtime = _runtime()
    acquired = runtime.hijack.acquire("owner", 60)
    assert acquired.session is not None
    hijack_id = acquired.session.hijack_id
    worker = _BlockingWorkerWs()
    runtime.worker_ws = worker

    delivery_task = asyncio.create_task(
        route_hijack(
            runtime,
            _Request({"keys": "owned-input"}),
            f"/worker/{runtime.worker_id}/hijack/{hijack_id}/send",
            "https://example.invalid/send",
            "POST",
        )
    )
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    assert runtime.hijack._session is not None
    runtime.hijack._session.lease_expires_at = 0
    expired_raw_session = runtime.hijack._session
    assert runtime.hijack.session is None
    assert runtime.hijack._session is expired_raw_session

    alarm_task = asyncio.create_task(runtime.alarm())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not alarm_task.done(), "lease expiry crossed an in-flight owned delivery"

    worker.release.set()
    await asyncio.wait_for(delivery_task, timeout=1)
    await asyncio.wait_for(alarm_task, timeout=1)
    assert runtime.hijack.session is None
    assert len(worker.sent) == 2


async def test_worker_replacement_waits_for_owned_delivery() -> None:
    runtime = _runtime()
    acquired = runtime.hijack.acquire("owner", 60)
    assert acquired.session is not None
    old_worker = _BlockingWorkerWs()
    runtime.worker_ws = old_worker

    delivery_task = asyncio.create_task(
        route_hijack(
            runtime,
            _Request({"keys": "owned-input"}),
            f"/worker/{runtime.worker_id}/hijack/{acquired.session.hijack_id}/send",
            "https://example.invalid/send",
            "POST",
        )
    )
    await asyncio.wait_for(old_worker.started.wait(), timeout=1)
    new_worker = _BlockingWorkerWs()
    new_worker.release.set()
    replacement_task = asyncio.create_task(runtime.register_worker_socket(new_worker))
    await asyncio.sleep(0)

    assert not replacement_task.done()
    assert runtime.worker_ws is old_worker

    old_worker.release.set()
    await asyncio.wait_for(delivery_task, timeout=1)
    await asyncio.wait_for(replacement_task, timeout=1)
    assert runtime.worker_ws is new_worker


async def test_worker_replacement_reapplies_active_pause_and_displaces_old_generation() -> None:
    runtime = _runtime()
    old_worker = _BlockingWorkerWs()
    old_worker.release.set()
    assert await runtime.register_worker_socket(old_worker)
    old_generation = runtime._worker_generation
    acquired = runtime.hijack.acquire("lease-owner", 60)
    assert acquired.session is not None
    runtime.persist_lease(acquired.session)

    new_worker = _BlockingWorkerWs()
    new_worker.release.set()
    assert await runtime.register_worker_socket(new_worker)

    assert runtime.worker_ws is new_worker
    assert runtime._worker_generation != old_generation
    assert _control(new_worker.sent[0])["action"] == "pause"
    assert old_worker.closed == (1012, "worker replaced")
    row = runtime.store.load_session(runtime.worker_id)
    assert row is not None and row["worker_generation"] == runtime._worker_generation


async def test_displaced_worker_frames_are_rejected() -> None:
    runtime = _runtime()
    old_worker = _BlockingWorkerWs()
    old_worker.release.set()
    assert await runtime.register_worker_socket(old_worker)
    new_worker = _BlockingWorkerWs()
    new_worker.release.set()
    assert await runtime.register_worker_socket(new_worker)
    browser = _BrowserWs()
    runtime.browser_sockets[runtime.ws_key(browser)] = browser

    await runtime.webSocketMessage(old_worker, frame_json("term", data="stale-output"))

    assert browser.sent == []
    assert old_worker.closed == (1008, "stale worker generation")
    assert runtime.worker_ws is new_worker


async def test_current_worker_generation_accepts_a_rebound_edge_proxy() -> None:
    """CF may provide a new Python proxy for the same attached edge socket."""
    runtime = _runtime()
    original_proxy = _BlockingWorkerWs()
    original_proxy.release.set()
    assert await runtime.register_worker_socket(original_proxy)
    rebound_proxy = _BlockingWorkerWs()
    rebound_proxy.release.set()
    rebound_proxy.attachment = original_proxy.attachment

    assert await runtime.activate_worker_socket(rebound_proxy)
    assert runtime.worker_ws is rebound_proxy
    assert rebound_proxy.closed is None


async def test_same_generation_rebound_proxy_close_clears_worker_and_kv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close callbacks use the server generation, not Python proxy identity."""
    runtime = _runtime()
    original_proxy = _BlockingWorkerWs()
    original_proxy.release.set()
    assert await runtime.register_worker_socket(original_proxy)
    rebound_proxy = _BlockingWorkerWs()
    rebound_proxy.release.set()
    rebound_proxy.attachment = original_proxy.attachment
    assert await runtime.activate_worker_socket(rebound_proxy)
    kv_updates: list[dict[str, object]] = []

    async def record_kv(_env: object, _worker_id: str, **values: object) -> None:
        kv_updates.append(values)

    monkeypatch.setattr("provide.uterm.cloudflare.do.session_runtime.lifecycle.update_kv_session", record_kv)
    runtime.lifecycle_state = "running"

    await runtime.webSocketClose(original_proxy, 1000, "same edge socket closed")

    assert runtime.worker_ws is None
    assert runtime._worker_generation is None
    assert runtime.lifecycle_state == "stopped"
    assert kv_updates[-1]["connected"] is False


async def test_stale_worker_close_does_not_publish_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    old_worker = _BlockingWorkerWs()
    old_worker.release.set()
    assert await runtime.register_worker_socket(old_worker)
    new_worker = _BlockingWorkerWs()
    new_worker.release.set()
    assert await runtime.register_worker_socket(new_worker)
    broadcasts: list[dict[str, object]] = []

    async def record_broadcast(frame: dict[str, object]) -> None:
        broadcasts.append(frame)

    kv_updates: list[dict[str, object]] = []

    async def record_kv(_env: object, _worker_id: str, **values: object) -> None:
        kv_updates.append(values)

    runtime.broadcast_worker_frame = record_broadcast  # type: ignore[method-assign]
    monkeypatch.setattr("provide.uterm.cloudflare.do.session_runtime.lifecycle.update_kv_session", record_kv)
    runtime.lifecycle_state = "running"

    await runtime.webSocketClose(old_worker, 1000, "stale close")

    assert runtime.worker_ws is new_worker
    assert runtime.lifecycle_state == "running"
    assert broadcasts == []
    assert kv_updates == []


async def test_worker_disconnect_waits_for_owned_delivery() -> None:
    runtime = _runtime()
    worker = _BlockingWorkerWs()
    assert await runtime.register_worker_socket(worker)
    acquired = runtime.hijack.acquire("owner", 60)
    assert acquired.session is not None

    delivery_task = asyncio.create_task(
        route_hijack(
            runtime,
            _Request({"keys": "owned-input"}),
            f"/worker/{runtime.worker_id}/hijack/{acquired.session.hijack_id}/send",
            "https://example.invalid/send",
            "POST",
        )
    )
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    disconnect_task = asyncio.create_task(runtime.unregister_worker_socket(worker))
    await asyncio.sleep(0)

    assert not disconnect_task.done()
    assert runtime.worker_ws is worker

    worker.release.set()
    await asyncio.wait_for(delivery_task, timeout=1)
    assert await asyncio.wait_for(disconnect_task, timeout=1)
    assert runtime.worker_ws is None


async def test_worker_send_timeout_is_bounded_and_fails_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    acquired = runtime.hijack.acquire("owner", 60)
    assert acquired.session is not None
    worker = _BlockingWorkerWs()
    runtime.worker_ws = worker
    monkeypatch.setattr(
        "provide.uterm.cloudflare.do.session_runtime.io._WORKER_SEND_TIMEOUT_S",
        0.01,
    )

    response = await asyncio.wait_for(
        route_hijack(
            runtime,
            _Request({"keys": "owned-input"}),
            f"/worker/{runtime.worker_id}/hijack/{acquired.session.hijack_id}/send",
            "https://example.invalid/send",
            "POST",
        ),
        timeout=0.5,
    )

    assert getattr(response, "status", None) == 409
    assert runtime.worker_ws is None


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

    response = await route_hijack(
        runtime,
        _Request({"keys": "must-not-send", "expect_regex": "["}),
        f"/worker/{runtime.worker_id}/hijack/{active.session.hijack_id}/send",
        "https://example.invalid/send",
        "POST",
    )

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

    response = await route_hijack(
        runtime,
        _Request({"keys": "must-not-send", "expect_regex": unsafe_pattern}),
        f"/worker/{runtime.worker_id}/hijack/{active.session.hijack_id}/send",
        "https://example.invalid/send",
        "POST",
    )

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


def test_new_persistence_reads_fail_closed() -> None:
    runtime = _runtime()
    assert runtime.store.load_runtime_activation("missing") is None
    runtime.store._exec(
        "INSERT INTO tunnel_invite_state(worker_id, entry_json, updated_at) VALUES(?, ?, ?)",
        ("broken", "{", 0.0),
    )
    assert runtime.store.load_tunnel_invite_state("broken") is None


def test_recording_availability_fails_closed_on_store_error() -> None:
    store = SimpleNamespace(current_event_seq=lambda _worker_id: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _recording_available(SimpleNamespace(store=store, worker_id="worker")) is False


class _AttachmentWs(_BrowserWs):
    def __init__(self, attachment: str) -> None:
        super().__init__()
        self.attachment = attachment

    def deserializeAttachment(self) -> str:  # noqa: N802 - Cloudflare WebSocket API
        return self.attachment


def test_attachment_restore_rejects_non_mapping_and_foreign_resume_token() -> None:
    runtime = _runtime()
    assert runtime._attachment_data(_AttachmentWs('uterm-v2:["not", "a", "mapping"]')) == {}

    ws = _AttachmentWs("")
    runtime._serialize_socket_attachment(
        ws,
        role="browser",
        browser_role="admin",
        socket_id="foreign-browser",
        resume_token="foreign-token",
    )
    runtime._restore_browser_identity(ws)
    assert "foreign-browser" not in runtime.browser_resume_tokens


def test_worker_restore_preserves_explicit_display_name() -> None:
    runtime = _runtime()
    ws = _AttachmentWs("")
    runtime._serialize_socket_attachment(
        ws,
        role="worker",
        browser_role="admin",
        socket_id="worker-restored-worker",
        worker_generation="restored-generation",
    )
    runtime.worker_id = "default"
    runtime.meta["display_name"] = "explicit"
    runtime._restore_worker_id_from_socket(ws)
    assert runtime.worker_id == "fence-worker"
    assert runtime.meta["display_name"] == "explicit"


async def test_register_worker_idempotent_and_attachment_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    worker = _BlockingWorkerWs()
    worker.release.set()
    assert await runtime.register_worker_socket(worker)
    assert await runtime.register_worker_socket(worker)

    replacement = _BlockingWorkerWs()
    replacement.release.set()
    monkeypatch.setattr(
        runtime, "_serialize_socket_attachment", lambda *_args, **_kwargs: (_ for _ in ()).throw(TypeError())
    )
    assert await runtime.register_worker_socket(replacement)
    assert replacement._ut_role == "worker"
    assert replacement._ut_worker_generation == runtime._worker_generation


async def test_replacement_pause_failure_closes_candidate() -> None:
    runtime = _runtime()
    assert runtime.hijack.acquire("owner", 60).session is not None

    class FailingWorker(_BlockingWorkerWs):
        async def send(self, _data: str) -> None:
            raise RuntimeError("pause failed")

    worker = FailingWorker()
    assert not await runtime.register_worker_socket(worker)
    assert worker.closed == (1011, "failed to restore active lease")


async def test_ushell_control_and_input_fail_closed() -> None:
    runtime = _runtime()

    class FailingUshell:
        async def handle_control(self, _action: str) -> None:
            raise RuntimeError("control failed")

        async def handle_input(self, _data: str) -> list[dict[str, object]]:
            raise RuntimeError("input failed")

    runtime._ushell = FailingUshell()
    assert not await runtime.push_worker_control("pause", owner="owner", lease_s=1)
    assert not await runtime.push_worker_input("data")


async def test_failed_old_send_does_not_clear_replacement() -> None:
    runtime = _runtime()
    replacement = _BlockingWorkerWs()

    class ReplacingFailure(_BlockingWorkerWs):
        async def send(self, _data: str) -> None:
            runtime.worker_ws = replacement
            raise RuntimeError("old worker failed")

    runtime.worker_ws = ReplacingFailure()
    assert not await runtime._send_worker_frame({"type": "data", "data": "x"})
    assert runtime.worker_ws is replacement


async def test_lifecycle_rejects_worker_activation_and_removes_unknown_sockets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    worker = _AttachmentWs("worker:admin:fence-worker")

    async def reject(_ws: object) -> bool:
        return False

    monkeypatch.setattr(runtime, "activate_worker_socket", reject)
    await runtime.webSocketOpen(worker)
    assert runtime.lifecycle_state == "stopped"

    unknown = _AttachmentWs("")
    unknown._ut_role = "unknown"
    runtime._register_socket(unknown, "unknown")
    await runtime.webSocketClose(unknown, 1000, "closed")
    runtime._register_socket(unknown, "unknown")
    await runtime.webSocketError(unknown, RuntimeError("failed"))


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


async def test_worker_upgrade_rejects_failed_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    runtime.ctx.acceptWebSocket = lambda _ws: None
    runtime.register_worker_socket = AsyncMock(return_value=False)
    client = SimpleNamespace()
    server = _BlockingWorkerWs()
    pair = SimpleNamespace(object_values=lambda: (client, server))
    js = ModuleType("js")
    js.WebSocketPair = SimpleNamespace(new=lambda: pair)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "js", js)
    request = SimpleNamespace(
        url=f"https://example.invalid/ws/worker/{runtime.worker_id}/term",
        method="GET",
        headers={"Upgrade": "websocket", "Authorization": f"Bearer {runtime.config.worker_bearer_token}"},
    )

    response = await runtime._fetch_impl(request)

    assert response.status == 409


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


async def test_failed_browser_broadcast_removal_waits_for_owned_delivery() -> None:
    runtime = _runtime()
    owner = _FailingBrowserWs()
    owner_key = runtime.ws_key(owner)
    runtime.browser_sockets[owner_key] = owner
    runtime.store.create_resume_token("failed-browser-token", runtime.worker_id, "admin", 60)
    runtime.browser_resume_tokens[owner_key] = "failed-browser-token"
    acquired = runtime.hijack.acquire("browser:failed-browser-token", 60)
    assert acquired.session is not None
    runtime.browser_hijack_owner[owner_key] = acquired.session.hijack_id
    worker = _BlockingWorkerWs()
    runtime.worker_ws = worker

    delivery_task = asyncio.create_task(
        route_hijack(
            runtime,
            _Request({"keys": "owned-input"}),
            f"/worker/{runtime.worker_id}/hijack/{acquired.session.hijack_id}/send",
            "https://example.invalid/send",
            "POST",
        )
    )
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    failed_broadcast = asyncio.create_task(runtime.broadcast_hijack_state())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not failed_broadcast.done()
    assert runtime.hijack.session is not None

    worker.release.set()
    await asyncio.wait_for(delivery_task, timeout=1)
    await asyncio.wait_for(failed_broadcast, timeout=1)
    assert runtime.hijack.session is None
    record = runtime.store.get_resume_token("failed-browser-token")
    assert record is not None and record["was_hijack_owner"] is True


async def test_session_delete_waits_for_owned_delivery() -> None:
    runtime = _runtime()
    acquired = runtime.hijack.acquire("owner", 60)
    assert acquired.session is not None
    worker = _BlockingWorkerWs()
    runtime.worker_ws = worker

    delivery_task = asyncio.create_task(
        route_hijack(
            runtime,
            _Request({"keys": "owned-input"}),
            f"/worker/{runtime.worker_id}/hijack/{acquired.session.hijack_id}/send",
            "https://example.invalid/send",
            "POST",
        )
    )
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    delete_task = asyncio.create_task(
        route_http(
            runtime,
            _Request(
                url=f"https://example.invalid/api/sessions/{runtime.worker_id}",
                method="DELETE",
            ),
        )
    )
    await asyncio.sleep(0)

    assert not delete_task.done()
    assert runtime._deleted_at is None

    worker.release.set()
    await asyncio.wait_for(delivery_task, timeout=1)
    response = await asyncio.wait_for(delete_task, timeout=1)
    assert getattr(response, "status", None) == 200
    assert runtime.lifecycle_state == "deleted"
    assert runtime.worker_ws is None
    assert runtime.hijack.session is None
