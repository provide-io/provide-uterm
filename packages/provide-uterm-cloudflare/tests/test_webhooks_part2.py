#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for do/_webhooks.py, state/store.py webhook methods, and dispatch."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from types import SimpleNamespace
from typing import Any

import pytest
from provide.uterm.cloudflare.do._webhooks import (
    _deliver_webhook,
    fire_webhooks,
    route_webhooks,
)
from provide.uterm.cloudflare.state.store import SqliteStateStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store() -> SqliteStateStore:
    conn = sqlite3.connect(":memory:")
    store = SqliteStateStore(conn.execute)
    store.migrate()
    return store


class _Runtime:
    def __init__(self, store: SqliteStateStore, worker_id: str = "w1") -> None:
        self.store = store
        self.worker_id = worker_id
        self.meta: dict = {
            "display_name": self.worker_id,
            "connector_type": "unknown",
            "created_at": 0.0,
            "tags": [],
            "visibility": "public",
            "owner": None,
        }

    async def request_json(self, request: object) -> dict[str, Any]:
        return json.loads(getattr(request, "_body", "{}"))


def _req(url: str, *, method: str = "GET", body: dict | None = None) -> SimpleNamespace:
    ns = SimpleNamespace(url=url, method=method)
    ns._body = json.dumps(body) if body else "{}"
    return ns


# ---------------------------------------------------------------------------
# Store: webhook CRUD
# ---------------------------------------------------------------------------


def test_store_save_webhook_upsert() -> None:
    store = _make_store()
    store.save_webhook("wh1", "s1", "https://example.com/old")
    store.save_webhook("wh1", "s1", "https://example.com/new")
    webhooks = store.load_webhooks("s1")
    assert len(webhooks) == 1
    assert webhooks[0]["url"] == "https://example.com/new"


# ---------------------------------------------------------------------------
# _deliver_webhook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_webhook_posts_payload() -> None:
    calls: list[tuple[str, dict, dict]] = []

    async def mock_fetch(url: str, *, method: str, headers: dict, body: str) -> None:
        calls.append((url, json.loads(body), headers))

    payload = {"event": {"type": "snapshot"}, "session_id": "s1", "webhook_id": "wh1", "timestamp": time.time()}
    await _deliver_webhook("https://example.com/hook", payload, None, _fetch=mock_fetch)

    assert len(calls) == 1
    assert calls[0][0] == "https://example.com/hook"
    assert calls[0][1]["session_id"] == "s1"
    assert "content-type" in calls[0][2]


@pytest.mark.asyncio
async def test_deliver_webhook_hmac_signature() -> None:
    calls: list[tuple[str, dict, dict]] = []

    async def mock_fetch(url: str, *, method: str, headers: dict, body: str) -> None:
        calls.append((url, json.loads(body), headers))

    secret = "uterm-test-secret-32-byte-minimum-key"
    payload = {"event": {}, "session_id": "s1", "webhook_id": "wh1", "timestamp": 1234.0}
    await _deliver_webhook("https://example.com/hook", payload, secret, _fetch=mock_fetch)

    _, _, headers = calls[0]
    sig = headers.get("x-uterm-signature", "")
    assert sig.startswith("sha256=")
    body_bytes = json.dumps(payload, ensure_ascii=True).encode()
    expected_hex = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
    assert sig == f"sha256={expected_hex}"


@pytest.mark.asyncio
async def test_deliver_webhook_no_signature_when_no_secret() -> None:
    calls: list[tuple[str, dict, dict]] = []

    async def mock_fetch(url: str, *, method: str, headers: dict, body: str) -> None:
        calls.append((url, json.loads(body), headers))

    await _deliver_webhook("https://example.com/hook", {}, None, _fetch=mock_fetch)
    _, _, headers = calls[0]
    assert "x-uterm-signature" not in headers


@pytest.mark.asyncio
async def test_deliver_webhook_fetch_error_logged_not_raised() -> None:
    async def bad_fetch(url: str, *, method: str, headers: dict, body: str) -> None:
        raise RuntimeError("network error")

    # Should not raise — errors are logged and swallowed
    await _deliver_webhook("https://example.com/hook", {}, None, _fetch=bad_fetch)


@pytest.mark.asyncio
async def test_deliver_webhook_uses_module_level_fetch_when_no_fetch_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    """When _fetch is None and _outbound_fetch is set, uses _outbound_fetch."""
    import provide.uterm.cloudflare.do._webhooks as wh_mod

    calls: list[str] = []

    async def mock_fetch(url: str, *, method: str, headers: dict, body: str) -> None:
        calls.append(url)

    monkeypatch.setattr(wh_mod, "_outbound_fetch", mock_fetch)
    await _deliver_webhook("https://example.com/hook", {}, None)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_deliver_webhook_no_fetch_and_no_js_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no fetch available and js is not importable, silently skips."""
    import provide.uterm.cloudflare.do._webhooks as wh_mod

    monkeypatch.setattr(wh_mod, "_outbound_fetch", None)
    # No _fetch provided, no js module → should return without raising
    await _deliver_webhook("https://example.com/hook", {}, None)


# ---------------------------------------------------------------------------
# fire_webhooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_webhooks_no_webhooks() -> None:
    store = _make_store()
    runtime = _Runtime(store, worker_id="w1")
    calls: list[str] = []

    async def mock_fetch(url: str, **_: Any) -> None:
        calls.append(url)

    await fire_webhooks(runtime, {"type": "snapshot", "seq": 1, "data": {}}, _fetch=mock_fetch)
    assert calls == []


@pytest.mark.asyncio
async def test_fire_webhooks_delivers_matching_event() -> None:
    store = _make_store()
    store.save_webhook("wh1", "w1", "https://example.com/hook")
    runtime = _Runtime(store, worker_id="w1")
    calls: list[dict] = []

    async def mock_fetch(url: str, *, method: str, headers: dict, body: str) -> None:
        calls.append(json.loads(body))

    await fire_webhooks(runtime, {"type": "snapshot", "seq": 1, "data": {}}, _fetch=mock_fetch)
    assert len(calls) == 1
    assert calls[0]["session_id"] == "w1"
    assert calls[0]["event"]["type"] == "snapshot"


@pytest.mark.asyncio
async def test_fire_webhooks_event_types_filter_excludes() -> None:
    store = _make_store()
    store.save_webhook("wh1", "w1", "https://example.com/hook", event_types=["hijack_acquired"])
    runtime = _Runtime(store, worker_id="w1")
    calls: list[str] = []

    async def mock_fetch(url: str, **_: Any) -> None:
        calls.append(url)

    await fire_webhooks(runtime, {"type": "snapshot", "seq": 1, "data": {}}, _fetch=mock_fetch)
    assert calls == []


@pytest.mark.asyncio
async def test_fire_webhooks_event_types_filter_includes() -> None:
    store = _make_store()
    store.save_webhook("wh1", "w1", "https://example.com/hook", event_types=["snapshot"])
    runtime = _Runtime(store, worker_id="w1")
    calls: list[str] = []

    async def mock_fetch(url: str, **_: Any) -> None:
        calls.append(url)

    await fire_webhooks(runtime, {"type": "snapshot", "seq": 1, "data": {}}, _fetch=mock_fetch)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_fire_webhooks_pattern_filter_on_snapshot() -> None:
    store = _make_store()
    store.save_webhook("wh1", "w1", "https://example.com/hook", pattern=r"\$\s")
    runtime = _Runtime(store, worker_id="w1")
    calls: list[str] = []

    async def mock_fetch(url: str, **_: Any) -> None:
        calls.append(url)

    # Non-matching screen
    await fire_webhooks(runtime, {"type": "snapshot", "screen": "loading...", "seq": 1, "data": {}}, _fetch=mock_fetch)
    assert calls == []

    # Matching screen
    await fire_webhooks(
        runtime, {"type": "snapshot", "screen": "root@host:~$ ", "seq": 2, "data": {}}, _fetch=mock_fetch
    )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_fire_webhooks_pattern_filter_on_data_screen() -> None:
    """Screen in data.screen (nested) also matched."""
    store = _make_store()
    store.save_webhook("wh1", "w1", "https://example.com/hook", pattern=r"\$")
    runtime = _Runtime(store, worker_id="w1")
    calls: list[str] = []

    async def mock_fetch(url: str, **_: Any) -> None:
        calls.append(url)

    await fire_webhooks(runtime, {"type": "snapshot", "seq": 1, "data": {"screen": "$ prompt"}}, _fetch=mock_fetch)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_fire_webhooks_pattern_not_applied_to_non_snapshot() -> None:
    """Pattern filter only applies to snapshot events; other types pass through."""
    store = _make_store()
    store.save_webhook("wh1", "w1", "https://example.com/hook", pattern=r"\$\s")
    runtime = _Runtime(store, worker_id="w1")
    calls: list[str] = []

    async def mock_fetch(url: str, **_: Any) -> None:
        calls.append(url)

    # Non-snapshot with no matching screen — pattern NOT applied
    await fire_webhooks(runtime, {"type": "hijack_acquired", "seq": 1, "data": {}}, _fetch=mock_fetch)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_fire_webhooks_invalid_pattern_skipped() -> None:
    """Invalid regex in pattern causes that webhook to be skipped (no crash)."""
    store = _make_store()
    store.save_webhook("wh1", "w1", "https://example.com/hook", pattern="[invalid")
    runtime = _Runtime(store, worker_id="w1")
    calls: list[str] = []

    async def mock_fetch(url: str, **_: Any) -> None:
        calls.append(url)

    await fire_webhooks(runtime, {"type": "snapshot", "screen": "anything", "seq": 1, "data": {}}, _fetch=mock_fetch)
    assert calls == []


# ---------------------------------------------------------------------------
# route_webhooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_webhooks_session_not_found() -> None:
    store = _make_store()
    runtime = _Runtime(store, worker_id="w1")
    req = _req("http://example.com/api/sessions/wrong/webhooks", method="POST", body={"url": "https://x.com/hook"})
    resp = await route_webhooks(runtime, req, "/api/sessions/wrong/webhooks", str(req.url), "POST", "wrong")
    data = json.loads(resp.body)
    assert resp.status == 404
    assert data["error"] == "not_found"


@pytest.mark.asyncio
async def test_route_webhooks_register_success() -> None:
    store = _make_store()
    runtime = _Runtime(store, worker_id="w1")
    req = _req(
        "http://example.com/api/sessions/w1/webhooks",
        method="POST",
        body={"url": "https://example.com/hook", "event_types": ["snapshot"]},
    )
    resp = await route_webhooks(runtime, req, "/api/sessions/w1/webhooks", str(req.url), "POST", "w1")
    data = json.loads(resp.body)
    assert resp.status == 200
    assert "webhook_id" in data
    assert data["session_id"] == "w1"
    assert data["url"] == "https://example.com/hook"

    # Verify persisted
    webhooks = store.load_webhooks("w1")
    assert len(webhooks) == 1


@pytest.mark.asyncio
async def test_route_webhooks_register_missing_url() -> None:
    store = _make_store()
    runtime = _Runtime(store, worker_id="w1")
    req = _req("http://example.com/api/sessions/w1/webhooks", method="POST", body={})
    resp = await route_webhooks(runtime, req, "/api/sessions/w1/webhooks", str(req.url), "POST", "w1")
    assert resp.status == 422


@pytest.mark.asyncio
async def test_route_webhooks_register_bad_event_types() -> None:
    store = _make_store()
    runtime = _Runtime(store, worker_id="w1")
    req = _req(
        "http://example.com/api/sessions/w1/webhooks",
        method="POST",
        body={"url": "https://x.com/hook", "event_types": "snapshot"},
    )
    resp = await route_webhooks(runtime, req, "/api/sessions/w1/webhooks", str(req.url), "POST", "w1")
    assert resp.status == 422


@pytest.mark.asyncio
async def test_route_webhooks_register_pattern_too_long() -> None:
    store = _make_store()
    runtime = _Runtime(store, worker_id="w1")
    req = _req(
        "http://example.com/api/sessions/w1/webhooks",
        method="POST",
        body={"url": "https://x.com/hook", "pattern": "a" * 257},
    )
    resp = await route_webhooks(runtime, req, "/api/sessions/w1/webhooks", str(req.url), "POST", "w1")
    assert resp.status == 422
    assert store.load_webhooks("w1") == []


@pytest.mark.asyncio
async def test_route_webhooks_register_pattern_not_string() -> None:
    store = _make_store()
    runtime = _Runtime(store, worker_id="w1")
    req = _req(
        "http://example.com/api/sessions/w1/webhooks",
        method="POST",
        body={"url": "https://x.com/hook", "pattern": 123},
    )
    resp = await route_webhooks(runtime, req, "/api/sessions/w1/webhooks", str(req.url), "POST", "w1")
    assert resp.status == 422


@pytest.mark.asyncio
async def test_route_webhooks_register_pattern_invalid_regex() -> None:
    store = _make_store()
    runtime = _Runtime(store, worker_id="w1")
    req = _req(
        "http://example.com/api/sessions/w1/webhooks",
        method="POST",
        body={"url": "https://x.com/hook", "pattern": "("},  # unbalanced group
    )
    resp = await route_webhooks(runtime, req, "/api/sessions/w1/webhooks", str(req.url), "POST", "w1")
    assert resp.status == 422


@pytest.mark.asyncio
async def test_route_webhooks_register_valid_pattern_persists() -> None:
    store = _make_store()
    runtime = _Runtime(store, worker_id="w1")
    req = _req(
        "http://example.com/api/sessions/w1/webhooks",
        method="POST",
        body={"url": "https://x.com/hook", "pattern": "ERROR|WARN"},
    )
    resp = await route_webhooks(runtime, req, "/api/sessions/w1/webhooks", str(req.url), "POST", "w1")
    assert resp.status == 200
    webhooks = store.load_webhooks("w1")
    assert len(webhooks) == 1
    assert webhooks[0]["pattern"] == "ERROR|WARN"


@pytest.mark.asyncio
async def test_route_webhooks_list() -> None:
    store = _make_store()
    store.save_webhook("wh1", "w1", "https://example.com/a")
    store.save_webhook("wh2", "w1", "https://example.com/b")
    runtime = _Runtime(store, worker_id="w1")
    req = _req("http://example.com/api/sessions/w1/webhooks", method="GET")
    resp = await route_webhooks(runtime, req, "/api/sessions/w1/webhooks", str(req.url), "GET", "w1")
    data = json.loads(resp.body)
    assert resp.status == 200
    assert len(data["webhooks"]) == 2


@pytest.mark.asyncio
async def test_route_webhooks_delete_success() -> None:
    store = _make_store()
    store.save_webhook("wh1", "w1", "https://example.com/hook")
    runtime = _Runtime(store, worker_id="w1")
    req = _req("http://example.com/api/sessions/w1/webhooks/wh1", method="DELETE")
    resp = await route_webhooks(runtime, req, "/api/sessions/w1/webhooks/wh1", str(req.url), "DELETE", "w1", "wh1")
    data = json.loads(resp.body)
    assert resp.status == 200
    assert data["ok"] is True
    assert store.load_webhooks("w1") == []


@pytest.mark.asyncio
async def test_route_webhooks_delete_not_found() -> None:
    store = _make_store()
    runtime = _Runtime(store, worker_id="w1")
    req = _req("http://example.com/api/sessions/w1/webhooks/nonexistent", method="DELETE")
    resp = await route_webhooks(
        runtime, req, "/api/sessions/w1/webhooks/nonexistent", str(req.url), "DELETE", "w1", "nonexistent"
    )
    assert resp.status == 404


@pytest.mark.asyncio
async def test_route_webhooks_unknown_method() -> None:
    store = _make_store()
    runtime = _Runtime(store, worker_id="w1")
    req = _req("http://example.com/api/sessions/w1/webhooks", method="PATCH")
    resp = await route_webhooks(runtime, req, "/api/sessions/w1/webhooks", str(req.url), "PATCH", "w1")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_route_webhooks_register_with_pattern_and_secret() -> None:
    store = _make_store()
    runtime = _Runtime(store, worker_id="w1")
    req = _req(
        "http://example.com/api/sessions/w1/webhooks",
        method="POST",
        body={"url": "https://example.com/hook", "pattern": r"\$", "secret": "mysecret"},
    )
    resp = await route_webhooks(runtime, req, "/api/sessions/w1/webhooks", str(req.url), "POST", "w1")
    assert resp.status == 200
    webhooks = store.load_webhooks("w1")
    assert webhooks[0]["pattern"] == r"\$"
    assert webhooks[0]["secret"] == "mysecret"


@pytest.mark.asyncio
async def test_route_webhooks_register_encrypts_secret_when_key_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """With WEBHOOK_SECRET_KEY set, the registered secret is stored encrypted
    (envelope), not as plaintext."""
    from provide.uterm.cloudflare.do import _webhook_crypto as wc

    async def fake_enc(_key: str, plaintext: bytes) -> tuple[bytes, bytes]:
        return (b"iviviviviviv", b"CT:" + plaintext)

    monkeypatch.setattr(wc, "_aesgcm_encrypt", fake_enc)
    store = _make_store()
    runtime = _Runtime(store, worker_id="w1")
    runtime.env = SimpleNamespace(WEBHOOK_SECRET_KEY="dGVzdGtleQ==")  # pragma: allowlist secret — base64("testkey")
    req = _req(
        "http://example.com/api/sessions/w1/webhooks",
        method="POST",
        body={"url": "https://example.com/hook", "secret": "mysecret"},
    )
    resp = await route_webhooks(runtime, req, "/api/sessions/w1/webhooks", str(req.url), "POST", "w1")
    assert resp.status == 200
    stored = store.load_webhooks("w1")[0]["secret"]
    assert stored.startswith("enc:v1:")
    assert "mysecret" not in stored


# ---------------------------------------------------------------------------
# Dispatch integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_sse_route() -> None:
    """SSE route dispatches correctly via route_http."""
    store = _make_store()
    store.append_event("w1", "snapshot", {"screen": "$ test"})

    from provide.uterm.cloudflare.api.http_routes import route_http
    from provide.uterm.cloudflare.bridge.hijack import HijackCoordinator

    class _FullRuntime:
        worker_id = "w1"
        worker_ws = None
        hijack = HijackCoordinator()
        last_snapshot: dict | None = None
        last_analysis: str | None = None
        lifecycle_state = "stopped"
        input_mode = "hijack"
        browser_hijack_owner: dict = {}
        meta: dict = {"visibility": "public", "owner": None}

        def __init__(self, s: SqliteStateStore) -> None:
            self.store = s

        async def browser_role_for_request(self, request: object) -> str:
            return "admin"

        async def browser_subject_for_request(self, request: object) -> str | None:
            return None

        async def request_json(self, request: object) -> dict:
            return {}

        def persist_lease(self, session: object) -> None:
            pass

        def clear_lease(self) -> None:
            pass

        async def push_worker_control(self, action: str, *, owner: str, lease_s: int) -> bool:
            return False

        async def broadcast_hijack_state(self) -> None:
            pass

        async def push_worker_input(self, data: str) -> bool:
            return False

        async def send_ws(self, ws: object, frame: dict) -> None:
            pass

        def ws_key(self, ws: object) -> str:
            return str(id(ws))

        def _socket_browser_role(self, ws: object) -> str:
            return "admin"

    runtime = _FullRuntime(store)
    req = SimpleNamespace(url="http://example.com/api/sessions/w1/events/stream", method="GET", headers={})
    resp = await route_http(runtime, req)  # type: ignore[arg-type]
    assert resp.status == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_dispatch_webhook_register_route() -> None:
    """Webhook POST route dispatches correctly via route_http."""
    store = _make_store()

    from provide.uterm.cloudflare.api.http_routes import route_http
    from provide.uterm.cloudflare.bridge.hijack import HijackCoordinator

    class _FullRuntime:
        worker_id = "w1"
        worker_ws = None
        hijack = HijackCoordinator()
        last_snapshot: dict | None = None
        last_analysis: str | None = None
        input_mode = "hijack"
        browser_hijack_owner: dict = {}
        meta: dict = {"visibility": "public", "owner": None}

        def __init__(self, s: SqliteStateStore) -> None:
            self.store = s

        async def browser_role_for_request(self, request: object) -> str:
            return "admin"

        async def browser_subject_for_request(self, request: object) -> str | None:
            return None

        async def request_json(self, request: object) -> dict:
            return json.loads(getattr(request, "_body", "{}"))

        def persist_lease(self, session: object) -> None:
            pass

        def clear_lease(self) -> None:
            pass

        async def push_worker_control(self, action: str, *, owner: str, lease_s: int) -> bool:
            return False

        async def broadcast_hijack_state(self) -> None:
            pass

        async def push_worker_input(self, data: str) -> bool:
            return False

        async def send_ws(self, ws: object, frame: dict) -> None:
            pass

        def ws_key(self, ws: object) -> str:
            return str(id(ws))

        def _socket_browser_role(self, ws: object) -> str:
            return "admin"

    runtime = _FullRuntime(store)
    req = SimpleNamespace(
        url="http://example.com/api/sessions/w1/webhooks",
        method="POST",
        _body=json.dumps({"url": "https://example.com/hook"}),
    )
    resp = await route_http(runtime, req)  # type: ignore[arg-type]
    assert resp.status == 200
    data = json.loads(resp.body)
    assert "webhook_id" in data
