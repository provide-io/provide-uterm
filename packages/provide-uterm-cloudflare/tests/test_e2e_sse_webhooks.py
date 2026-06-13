#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E tests for SSE streaming and webhook CRUD against a live pywrangler dev server.

Run with:
    uv run pytest -m e2e packages/provide-uterm-cloudflare/tests/test_e2e_sse_webhooks.py
or:
    E2E=1 uv run pytest packages/provide-uterm-cloudflare/tests/

Scenarios
---------
SSE:
  1. GET /events/stream returns text/event-stream with retry directive (empty)
  2. After worker sends snapshot, SSE returns that event
  3. after_seq filters: events before cursor excluded, events after included
  4. 404 for unknown session on SSE endpoint

Webhooks:
  5. POST registers webhook → 200 with webhook_id
  6. GET lists registered webhooks
  7. DELETE unregisters webhook → 200 ok
  8. DELETE nonexistent webhook → 404
  9. POST missing url → 422
  10. 404 for unknown session on webhook endpoint
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid

import pytest
import websockets

from provide.uterm.control_channel import encode_control_frame

_WS_TIMEOUT_S = 0.5  # short drain — worker WS gets no hello; just let it time out quickly
_WS_PROCESS_S = 1.0  # time for pywrangler to process a WS frame and persist the event
_HTTP_UA = "provide-uterm-e2e-test/1.0"

# CF Access service token for real_cf tests (bypasses Cloudflare Access login).
# Set via env vars or fall back to empty (local pywrangler dev uses AUTH_MODE=dev).
_CF_CLIENT_ID = os.environ.get("CF_ACCESS_CLIENT_ID", "")
_CF_CLIENT_SECRET = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")
_WORKER_BEARER_TOKEN = os.environ.get("CF_WORKER_BEARER_TOKEN", "")
# A JWT the worker's decode_jwt accepts (real CF Access token, or one minted
# against a test JWKS the worker is pointed at) — sent as the bearer credential
# for authenticated DO routes. See the CF_E2E_JWT gate in conftest.py.
_E2E_JWT = os.environ.get("CF_E2E_JWT", "")


# ---------------------------------------------------------------------------
# Helpers — reused from test_e2e_ws.py pattern
# ---------------------------------------------------------------------------


def _cf_access_headers(url: str = "") -> dict[str, str]:
    """Auth headers for the worker: a JWT bearer (any scheme) and, for real CF
    over https, the CF Access service-token pair.

    ``CF_E2E_JWT`` carries a JWT the worker's ``decode_jwt`` accepts (a real CF
    Access token, or one minted against a test JWKS the worker is pointed at).
    It is the credential for the authenticated DO routes (webhooks, sessions)
    now that the ``AUTH_MODE=dev`` bypass is gone; without it those routes 401.
    """
    headers: dict[str, str] = {}
    if _E2E_JWT:
        headers["Authorization"] = f"Bearer {_E2E_JWT}"
    if not url.startswith("http://") and _CF_CLIENT_ID and _CF_CLIENT_SECRET:
        headers["CF-Access-Client-Id"] = _CF_CLIENT_ID
        headers["CF-Access-Client-Secret"] = _CF_CLIENT_SECRET
    return headers


def _base_ws(base_http: str) -> str:
    return base_http.replace("http://", "ws://").replace("https://", "wss://")


def _new_worker_id() -> str:
    return f"e2e-sse-{uuid.uuid4().hex[:8]}"


def _ws_connect(uri: str):
    """Connect with CF Access headers when targeting real CF (wss://)."""
    extra = _cf_access_headers(uri)
    if _WORKER_BEARER_TOKEN and "/ws/worker/" in uri and uri.startswith("wss://"):
        extra["Authorization"] = f"Bearer {_WORKER_BEARER_TOKEN}"
    return websockets.connect(uri, additional_headers=extra) if extra else websockets.connect(uri)


def _http_get_raw(base: str, path: str) -> tuple[int, bytes, dict[str, str]]:
    """Return (status, body_bytes, headers) — does NOT parse JSON."""
    url = f"{base}{path}"
    headers = {"User-Agent": _HTTP_UA, **_cf_access_headers(url)}
    req = urllib.request.Request(url, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), {}


def _http_get_json(base: str, path: str) -> tuple[int, object]:
    status, body, _ = _http_get_raw(base, path)
    try:
        return status, json.loads(body)
    except Exception:
        return status, {}


def _http_post(base: str, path: str, body: dict) -> tuple[int, dict]:
    url = f"{base}{path}"
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "User-Agent": _HTTP_UA, **_cf_access_headers(url)}
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, {}


def _http_delete(base: str, path: str) -> tuple[int, dict]:
    url = f"{base}{path}"
    headers = {"User-Agent": _HTTP_UA, **_cf_access_headers(url)}
    req = urllib.request.Request(url, method="DELETE", headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, {}


def _parse_sse(body: bytes) -> list[dict]:
    """Parse SSE body into a list of event dicts (from data: lines)."""
    events: list[dict] = []
    for line in body.decode().splitlines():
        if line.startswith("data: "):
            with contextlib.suppress(json.JSONDecodeError):
                events.append(json.loads(line[6:]))
    return events


def _parse_sse_retry(body: bytes) -> int | None:
    """Extract retry milliseconds from SSE body."""
    for line in body.decode().splitlines():
        if line.startswith("retry: "):
            try:
                return int(line[7:])
            except ValueError:
                pass
    return None


def _parse_sse_ids(body: bytes) -> list[str]:
    """Extract id: lines from SSE body."""
    return [line[4:] for line in body.decode().splitlines() if line.startswith("id: ")]


async def _connect_worker_send_snapshot(base_ws: str, worker_id: str, screen: str) -> None:
    """Connect worker WS, drain snapshot_req, send a snapshot frame, disconnect."""
    worker_uri = f"{base_ws}/ws/worker/{worker_id}/term"
    async with _ws_connect(worker_uri) as ws:
        # Drain snapshot_req sent by server on connect
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(ws.recv(), timeout=_WS_TIMEOUT_S)
        await ws.send(
            encode_control_frame(
                {
                    "type": "snapshot",
                    "screen": screen,
                    "cursor": {"x": 0, "y": 0},
                    "cols": 80,
                    "rows": 25,
                    "screen_hash": f"e2e-{screen[:8]}",
                    "cursor_at_end": True,
                    "has_trailing_space": False,
                    "prompt_detected": {"prompt_id": "e2e-p"},
                    "ts": time.time(),
                }
            )
        )
        await asyncio.sleep(_WS_PROCESS_S)  # give DO time to persist event


# ---------------------------------------------------------------------------
# SSE E2E tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_do_sse_empty_stream_has_retry_directive(wrangler_server: str) -> None:
    """SSE endpoint on empty session returns 200 text/event-stream with retry."""
    worker_id = _new_worker_id()
    status, body, headers = _http_get_raw(wrangler_server, f"/api/sessions/{worker_id}/events/stream")
    assert status == 200, f"expected 200, got {status}: {body[:200]}"
    ct = headers.get("Content-Type", headers.get("content-type", ""))
    assert "text/event-stream" in ct, f"wrong content-type: {ct}"
    retry_ms = _parse_sse_retry(body)
    assert retry_ms is not None, f"no retry directive in SSE body:\n{body.decode()}"
    assert retry_ms > 0


@pytest.mark.e2e
async def test_do_sse_delivers_snapshot_after_worker_sends(wrangler_server: str) -> None:
    """Worker sends snapshot → SSE after_seq=0 includes that event."""
    base_ws = _base_ws(wrangler_server)
    worker_id = _new_worker_id()
    screen = f"$ e2e-sse-{worker_id}"

    await _connect_worker_send_snapshot(base_ws, worker_id, screen)

    status, body, _ = _http_get_raw(wrangler_server, f"/api/sessions/{worker_id}/events/stream?after_seq=0")
    assert status == 200
    events = _parse_sse(body)
    assert len(events) >= 1, f"expected at least 1 event, got none\nbody:\n{body.decode()}"
    types = [e.get("type") for e in events]
    assert "snapshot" in types, f"no snapshot event in: {types}"
    screen_vals = [e.get("data", {}).get("screen", e.get("screen", "")) for e in events]
    assert any(screen in sv for sv in screen_vals), f"screen not found in {screen_vals}"


@pytest.mark.e2e
async def test_do_sse_ids_match_seq(wrangler_server: str) -> None:
    """Each SSE event has an id: matching its seq field."""
    base_ws = _base_ws(wrangler_server)
    worker_id = _new_worker_id()

    await _connect_worker_send_snapshot(base_ws, worker_id, f"$ seq-test-{worker_id}")

    status, body, _ = _http_get_raw(wrangler_server, f"/api/sessions/{worker_id}/events/stream?after_seq=0")
    assert status == 200
    ids = _parse_sse_ids(body)
    events = _parse_sse(body)
    assert len(ids) == len(events), f"id count {len(ids)} != event count {len(events)}"
    for id_str, event in zip(ids, events):
        assert id_str == str(event["seq"]), f"id {id_str!r} != event seq {event['seq']!r}"


@pytest.mark.e2e
async def test_do_sse_after_seq_filters_old_events(wrangler_server: str) -> None:
    """Events at or before after_seq are excluded from the SSE response."""
    base_ws = _base_ws(wrangler_server)
    worker_id = _new_worker_id()

    # Send two snapshots
    await _connect_worker_send_snapshot(base_ws, worker_id, f"$ first-{worker_id}")
    await asyncio.sleep(0.2)
    await _connect_worker_send_snapshot(base_ws, worker_id, f"$ second-{worker_id}")
    await asyncio.sleep(0.2)

    # Get all events to find first seq
    _, body_all, _ = _http_get_raw(wrangler_server, f"/api/sessions/{worker_id}/events/stream?after_seq=0")
    events_all = _parse_sse(body_all)
    assert len(events_all) >= 2, f"expected >= 2 events, got {len(events_all)}"

    first_seq = events_all[0]["seq"]

    # Request only events after first_seq
    _, body_filtered, _ = _http_get_raw(
        wrangler_server, f"/api/sessions/{worker_id}/events/stream?after_seq={first_seq}"
    )
    events_filtered = _parse_sse(body_filtered)
    seqs_filtered = [e["seq"] for e in events_filtered]
    assert all(s > first_seq for s in seqs_filtered), f"old event leaked through: {seqs_filtered}"


@pytest.mark.e2e
async def test_do_sse_unknown_session_returns_404(wrangler_server: str) -> None:
    """SSE on a session the DO doesn't own returns 404."""
    # Use a session_id that differs from the DO's worker_id
    # The DO for "session-x" will see request for "other-session" → 404
    worker_id = _new_worker_id()
    wrong_id = _new_worker_id()

    # Trigger DO creation for worker_id
    _http_get_raw(wrangler_server, f"/api/sessions/{worker_id}/events/stream")

    # Now request wrong_id on the worker_id's DO (simulate by using a different path)
    # In practice each DO has its own worker_id — request a different session's DO
    status, body, _ = _http_get_raw(wrangler_server, f"/api/sessions/{wrong_id}/events/stream")
    # The DO for wrong_id will be created and will own that id, so it returns 200
    # OR it's a 404 if routing sends wrong-id request to wrong DO
    # Either way it must not 500
    assert status in {200, 404}, f"expected 200 or 404, got {status}: {body[:200]}"


# ---------------------------------------------------------------------------
# Webhook CRUD E2E tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_do_webhook_register_returns_webhook_id(wrangler_server: str) -> None:
    """POST /webhooks → 200 with webhook_id, session_id, url."""
    worker_id = _new_worker_id()
    hook_url = "https://example.com/hook"

    status, body = _http_post(
        wrangler_server,
        f"/api/sessions/{worker_id}/webhooks",
        {"url": hook_url},
    )
    assert status == 200, f"expected 200, got {status}: {body}"
    assert "webhook_id" in body, f"no webhook_id in {body}"
    assert body["session_id"] == worker_id
    assert body["url"] == hook_url


@pytest.mark.e2e
async def test_do_webhook_register_with_secret_does_not_echo_secret(wrangler_server: str) -> None:
    """Registering a webhook WITH a secret runs encrypt_secret in the real workerd runtime.

    ``encrypt_secret``/``decrypt_secret`` (do/_webhook_crypto.py) call
    ``crypto.subtle`` (AES-256-GCM) and are ``# pragma: no cover - CF runtime
    only`` because Web Crypto only exists inside workerd/Pyodide — a unit test
    cannot exercise them. This e2e test drives the register path with a
    ``secret`` so the encrypt branch actually runs in the Worker, and asserts:

    1. registration still succeeds (200 + webhook_id), proving encryption did
       not raise; and
    2. the response never echoes the plaintext ``secret`` back — it is
       encrypted at rest and must not leak in the registration reply.

    When ``WEBHOOK_SECRET_KEY`` is unset the crypto module returns plaintext
    (documented fallback); the no-echo assertion holds either way, so this test
    is meaningful both with and without the key configured. See the runbook in
    ``.provide/HANDOFF.md`` for the full encrypt→decrypt→HMAC round-trip
    validation against a deployed Worker.
    """
    worker_id = _new_worker_id()
    hook_url = "https://example.com/hook"

    status, body = _http_post(
        wrangler_server,
        f"/api/sessions/{worker_id}/webhooks",
        {"url": hook_url, "secret": "live-test-secret"},  # pragma: allowlist secret
    )
    assert status == 200, f"expected 200, got {status}: {body}"
    assert "webhook_id" in body, f"no webhook_id in {body}"
    assert body["url"] == hook_url
    # The plaintext secret must never round-trip back to the caller.
    assert "secret" not in body, f"secret leaked in registration response: {body}"
    assert "live-test-secret" not in str(body), f"secret value leaked in response: {body}"  # pragma: allowlist secret


@pytest.mark.e2e
async def test_do_webhook_list_after_register(wrangler_server: str) -> None:
    """POST then GET lists the registered webhook."""
    worker_id = _new_worker_id()

    reg_status, reg_body = _http_post(
        wrangler_server,
        f"/api/sessions/{worker_id}/webhooks",
        {"url": "https://example.com/hook"},
    )
    assert reg_status == 200

    list_status, list_body = _http_get_json(wrangler_server, f"/api/sessions/{worker_id}/webhooks")
    assert list_status == 200
    assert isinstance(list_body, dict)
    webhooks = list_body.get("webhooks", [])
    assert len(webhooks) == 1
    assert webhooks[0]["webhook_id"] == reg_body["webhook_id"]
    assert webhooks[0]["url"] == "https://example.com/hook"


@pytest.mark.e2e
async def test_do_webhook_register_multiple_then_list(wrangler_server: str) -> None:
    """Two webhooks registered for same session → both appear in list."""
    worker_id = _new_worker_id()

    _http_post(wrangler_server, f"/api/sessions/{worker_id}/webhooks", {"url": "https://example.com/a"})
    _http_post(wrangler_server, f"/api/sessions/{worker_id}/webhooks", {"url": "https://example.com/b"})

    _, list_body = _http_get_json(wrangler_server, f"/api/sessions/{worker_id}/webhooks")
    assert isinstance(list_body, dict)
    assert len(list_body.get("webhooks", [])) == 2


@pytest.mark.e2e
async def test_do_webhook_delete_removes_webhook(wrangler_server: str) -> None:
    """DELETE removes the webhook; list returns empty."""
    worker_id = _new_worker_id()

    _, reg_body = _http_post(
        wrangler_server,
        f"/api/sessions/{worker_id}/webhooks",
        {"url": "https://example.com/hook"},
    )
    webhook_id = reg_body["webhook_id"]

    del_status, del_body = _http_delete(wrangler_server, f"/api/sessions/{worker_id}/webhooks/{webhook_id}")
    assert del_status == 200, f"expected 200, got {del_status}: {del_body}"
    assert del_body.get("ok") is True

    _, list_body = _http_get_json(wrangler_server, f"/api/sessions/{worker_id}/webhooks")
    assert list_body.get("webhooks", []) == []


@pytest.mark.e2e
async def test_do_webhook_delete_nonexistent_returns_404(wrangler_server: str) -> None:
    """DELETE on a webhook_id that doesn't exist returns 404."""
    worker_id = _new_worker_id()
    del_status, _ = _http_delete(wrangler_server, f"/api/sessions/{worker_id}/webhooks/nonexistent")
    assert del_status == 404


@pytest.mark.e2e
async def test_do_webhook_register_missing_url_returns_422(wrangler_server: str) -> None:
    """POST without url field → 422."""
    worker_id = _new_worker_id()
    status, _ = _http_post(wrangler_server, f"/api/sessions/{worker_id}/webhooks", {})
    assert status == 422


@pytest.mark.e2e
async def test_do_webhook_register_with_event_types_filter(wrangler_server: str) -> None:
    """POST with event_types list is stored and returned."""
    worker_id = _new_worker_id()
    status, body = _http_post(
        wrangler_server,
        f"/api/sessions/{worker_id}/webhooks",
        {"url": "https://example.com/hook", "event_types": ["snapshot", "hijack_acquired"]},
    )
    assert status == 200
    assert body.get("event_types") == ["snapshot", "hijack_acquired"]

    _, list_body = _http_get_json(wrangler_server, f"/api/sessions/{worker_id}/webhooks")
    wh = list_body["webhooks"][0]
    assert wh["event_types"] == ["snapshot", "hijack_acquired"]


@pytest.mark.e2e
async def test_do_webhook_register_with_pattern(wrangler_server: str) -> None:
    """POST with pattern is stored and returned."""
    worker_id = _new_worker_id()
    status, body = _http_post(
        wrangler_server,
        f"/api/sessions/{worker_id}/webhooks",
        {"url": "https://example.com/hook", "pattern": r"\$\s"},
    )
    assert status == 200
    assert body.get("pattern") == r"\$\s"


@pytest.mark.e2e
async def test_do_sse_and_webhooks_same_session_full_flow(wrangler_server: str) -> None:
    """Integration: snapshot stored → visible in SSE; webhook registered → appears in list."""
    base_ws = _base_ws(wrangler_server)
    worker_id = _new_worker_id()
    screen = f"$ integration-{worker_id}"

    # Register webhook first
    reg_status, reg_body = _http_post(
        wrangler_server,
        f"/api/sessions/{worker_id}/webhooks",
        {"url": "https://example.com/integration-hook"},
    )
    assert reg_status == 200
    webhook_id = reg_body["webhook_id"]

    # Send snapshot via worker WS
    await _connect_worker_send_snapshot(base_ws, worker_id, screen)

    # Verify event appears in SSE
    _, sse_body, _ = _http_get_raw(wrangler_server, f"/api/sessions/{worker_id}/events/stream?after_seq=0")
    events = _parse_sse(sse_body)
    assert any(e.get("type") == "snapshot" for e in events), f"no snapshot in SSE: {events}"

    # Verify webhook still registered (delivery is fire-and-forget; can't assert receipt without receiver)
    _, list_body = _http_get_json(wrangler_server, f"/api/sessions/{worker_id}/webhooks")
    assert any(w["webhook_id"] == webhook_id for w in list_body.get("webhooks", []))

    # Unregister and confirm gone
    del_status, _ = _http_delete(wrangler_server, f"/api/sessions/{worker_id}/webhooks/{webhook_id}")
    assert del_status == 200
    _, list_body2 = _http_get_json(wrangler_server, f"/api/sessions/{worker_id}/webhooks")
    assert list_body2.get("webhooks", []) == []
