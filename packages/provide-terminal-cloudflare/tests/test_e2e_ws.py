"""E2E WebSocket and REST tests for the CF package.

Verifies:
- Item 1: Fleet-Wide Session Registry (KV) — worker connect/disconnect reflected in /api/sessions
- Item 4: Alarm-Based Hijack Lease Expiry — expired leases are auto-released
- Item 5: /hijack/{id}/snapshot endpoint — snapshot stored and returned

Run with:
    uv run pytest -m e2e               # pywrangler dev tests only
    REAL_CF=1 uv run pytest -m e2e     # also runs tests that need real CF infrastructure

Tests marked @pytest.mark.real_cf require a real Cloudflare deployment with:
  - SESSION_REGISTRY KV namespace (real IDs in wrangler.toml, see terraform/)
  - Full WS hibernation API support (server→client push)
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
import uuid

import pytest
import websockets

_WS_TIMEOUT_S = 15.0
_HTTP_UA = "provide-terminal-e2e-test/1.0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_ws(base_http: str) -> str:
    return base_http.replace("http://", "ws://").replace("https://", "wss://")


def _new_worker_id() -> str:
    return f"e2e-{uuid.uuid4().hex[:8]}"


def _http_get(base: str, path: str) -> tuple[int, object]:
    url = f"{base}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": _HTTP_UA})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, {}


def _http_post(base: str, path: str, body: dict) -> tuple[int, dict]:
    url = f"{base}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": _HTTP_UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, {}


async def _recv_ws(ws, timeout: float = _WS_TIMEOUT_S) -> dict:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


async def _drain_until(ws, target_type: str, max_frames: int = 10) -> dict | None:
    for _ in range(max_frames):
        frame = await _recv_ws(ws)
        if frame.get("type") == target_type:
            return frame
    return None


# ---------------------------------------------------------------------------
# Tests — browser WS push (real_cf: needs full WS hibernation support)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.real_cf
async def test_browser_receives_hello_frame(wrangler_server: str) -> None:
    """Browser WS connection → server sends hello frame."""
    base_ws = _base_ws(wrangler_server)
    worker_id = _new_worker_id()
    uri = f"{base_ws}/ws/browser/{worker_id}/term"
    async with websockets.connect(uri) as browser_ws:
        hello = await _recv_ws(browser_ws)
    assert hello["type"] == "hello"
    assert "worker_online" in hello


@pytest.mark.e2e
@pytest.mark.real_cf
async def test_browser_receives_snapshot_after_worker_sends(wrangler_server: str) -> None:
    """Worker sends snapshot frame → browser receives it."""
    base_ws = _base_ws(wrangler_server)
    worker_id = _new_worker_id()
    worker_uri = f"{base_ws}/ws/worker/{worker_id}/term"
    browser_uri = f"{base_ws}/ws/browser/{worker_id}/term"
    snapshot_payload = json.dumps({"type": "snapshot", "screen": "hello-e2e", "ts": time.time()})

    async with websockets.connect(worker_uri) as worker_ws, websockets.connect(browser_uri) as browser_ws:
        # Drain the hello frame (sent synchronously in fetch() before 101).
        await _drain_until(browser_ws, "hello", max_frames=3)
        await worker_ws.send(snapshot_payload)
        received = await _drain_until(browser_ws, "snapshot", max_frames=5)

    assert received is not None, "browser did not receive snapshot frame"
    assert received.get("screen") == "hello-e2e"


# ---------------------------------------------------------------------------
# Tests — KV fleet registry (real_cf: needs real KV namespace IDs)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.real_cf
async def test_worker_connect_registers_in_kv(wrangler_server: str) -> None:
    """Worker WS connect → session appears in GET /api/sessions with connected=True."""
    base_ws = _base_ws(wrangler_server)
    worker_id = _new_worker_id()
    async with websockets.connect(f"{base_ws}/ws/worker/{worker_id}/term"):
        await asyncio.sleep(1.0)
        status, body = _http_get(wrangler_server, "/api/sessions")
    assert status == 200
    assert isinstance(body, list)
    assert len(body) >= 1, f"no sessions returned: {body}"
    assert any(s.get("connected") for s in body), f"no connected session in: {body}"


@pytest.mark.e2e
@pytest.mark.real_cf
async def test_worker_disconnect_removes_from_kv(wrangler_server: str) -> None:
    """Worker WS disconnect → no connected sessions in GET /api/sessions."""
    base_ws = _base_ws(wrangler_server)
    worker_id = _new_worker_id()
    async with websockets.connect(f"{base_ws}/ws/worker/{worker_id}/term"):
        await asyncio.sleep(0.3)
    await asyncio.sleep(1.0)
    status, body = _http_get(wrangler_server, "/api/sessions")
    assert status == 200
    assert isinstance(body, list)
    # Only check that THIS test's worker is no longer in the connected list.
    # Other stale KV entries from parallel/previous tests are ignored.
    still_connected = [s for s in body if s.get("connected") and s.get("session_id") == worker_id]
    assert not still_connected, f"session {worker_id} still connected after disconnect: {body}"


@pytest.mark.e2e
@pytest.mark.real_cf
async def test_fleet_sessions_lists_all_connected_workers(wrangler_server: str) -> None:
    """Two workers connected → both appear in GET /api/sessions."""
    base_ws = _base_ws(wrangler_server)
    worker_id_1 = _new_worker_id()
    worker_id_2 = _new_worker_id()

    async with (
        websockets.connect(f"{base_ws}/ws/worker/{worker_id_1}/term"),
        websockets.connect(f"{base_ws}/ws/worker/{worker_id_2}/term"),
    ):
        await asyncio.sleep(2.0)
        # Use asyncio.to_thread so the blocking HTTP call doesn't starve WS keepalives.
        status, body = await asyncio.to_thread(_http_get, wrangler_server, "/api/sessions")

    assert status == 200
    assert isinstance(body, list)
    ids = [s.get("session_id") for s in body]
    assert worker_id_1 in ids, f"{worker_id_1} not in sessions: {ids}"
    assert worker_id_2 in ids, f"{worker_id_2} not in sessions: {ids}"


# ---------------------------------------------------------------------------
# Tests — hijack REST API (pass against pywrangler dev)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_hijack_acquire_and_release(wrangler_server: str) -> None:
    """Acquire hijack → 200; release → 200; re-acquire with new owner → 200."""
    base_ws = _base_ws(wrangler_server)
    worker_id = _new_worker_id()
    async with websockets.connect(f"{base_ws}/ws/worker/{worker_id}/term"):
        status1, body1 = _http_post(
            wrangler_server, f"/worker/{worker_id}/hijack/acquire", {"owner": "e2e-owner", "lease_s": 30}
        )
        assert status1 == 200, f"first acquire: {body1}"
        hijack_id = body1["hijack_id"]

        status2, body2 = _http_post(wrangler_server, f"/worker/{worker_id}/hijack/{hijack_id}/release", {})
        assert status2 == 200, f"release: {body2}"

        status3, body3 = _http_post(
            wrangler_server, f"/worker/{worker_id}/hijack/acquire", {"owner": "e2e-owner-2", "lease_s": 10}
        )
        assert status3 == 200, f"re-acquire: {body3}"
        hijack_id2 = body3["hijack_id"]
        _http_post(wrangler_server, f"/worker/{worker_id}/hijack/{hijack_id2}/release", {})


@pytest.mark.e2e
async def test_hijack_conflict_returns_409(wrangler_server: str) -> None:
    """Two simultaneous acquires by different owners → second gets 409."""
    base_ws = _base_ws(wrangler_server)
    worker_id = _new_worker_id()
    async with websockets.connect(f"{base_ws}/ws/worker/{worker_id}/term"):
        status1, body1 = _http_post(
            wrangler_server, f"/worker/{worker_id}/hijack/acquire", {"owner": "owner-first", "lease_s": 30}
        )
        assert status1 == 200, f"first acquire: {body1}"
        hijack_id = body1["hijack_id"]

        status2, body2 = _http_post(
            wrangler_server, f"/worker/{worker_id}/hijack/acquire", {"owner": "owner-second", "lease_s": 30}
        )
        assert status2 == 409, f"expected 409, got {status2}: {body2}"
        _http_post(wrangler_server, f"/worker/{worker_id}/hijack/{hijack_id}/release", {})


@pytest.mark.e2e
async def test_hijack_snapshot_endpoint(wrangler_server: str) -> None:
    """Worker sends snapshot → GET /hijack/{id}/snapshot returns it."""
    base_ws = _base_ws(wrangler_server)
    worker_id = _new_worker_id()
    snapshot_payload = json.dumps({"type": "snapshot", "screen": "snapshot-e2e-content", "ts": time.time()})

    async with websockets.connect(f"{base_ws}/ws/worker/{worker_id}/term") as worker_ws:
        await worker_ws.send(snapshot_payload)
        await asyncio.sleep(0.5)

        status1, body1 = _http_post(
            wrangler_server, f"/worker/{worker_id}/hijack/acquire", {"owner": "snap-test", "lease_s": 30}
        )
        assert status1 == 200, f"acquire: {body1}"
        hijack_id = body1["hijack_id"]

        status2, body2 = _http_get(wrangler_server, f"/worker/{worker_id}/hijack/{hijack_id}/snapshot")
        assert status2 == 200, f"snapshot endpoint: {body2}"
        assert body2.get("ok") is True  # type: ignore[union-attr]
        snap = body2.get("snapshot")  # type: ignore[union-attr]
        assert snap is not None, "snapshot field missing"
        assert snap.get("screen") == "snapshot-e2e-content"  # type: ignore[union-attr]
        _http_post(wrangler_server, f"/worker/{worker_id}/hijack/{hijack_id}/release", {})


@pytest.mark.e2e
async def test_hijack_alarm_expiry(wrangler_server: str) -> None:
    """Acquire with 2s lease → wait 4s → second acquire succeeds (lease expired)."""
    base_ws = _base_ws(wrangler_server)
    worker_id = _new_worker_id()
    async with websockets.connect(f"{base_ws}/ws/worker/{worker_id}/term"):
        status1, body1 = _http_post(
            wrangler_server, f"/worker/{worker_id}/hijack/acquire", {"owner": "expiry-first", "lease_s": 2}
        )
        assert status1 == 200, f"acquire: {body1}"

        await asyncio.sleep(4.0)

        status2, body2 = _http_post(
            wrangler_server, f"/worker/{worker_id}/hijack/acquire", {"owner": "expiry-second", "lease_s": 10}
        )
        assert status2 == 200, f"post-expiry acquire failed: {body2}"
        hijack_id2 = body2["hijack_id"]
        _http_post(wrangler_server, f"/worker/{worker_id}/hijack/{hijack_id2}/release", {})


@pytest.mark.e2e
async def test_input_mode_change(wrangler_server: str) -> None:
    """POST /worker/{id}/input_mode → 200 with updated mode reflected in response."""
    base_ws = _base_ws(wrangler_server)
    worker_id = _new_worker_id()
    async with websockets.connect(f"{base_ws}/ws/worker/{worker_id}/term"):
        status, body = _http_post(wrangler_server, f"/worker/{worker_id}/input_mode", {"input_mode": "open"})
        assert status == 200, f"input_mode change: {body}"
        assert body.get("input_mode") == "open"
        assert body.get("ok") is True

        status2, body2 = _http_post(wrangler_server, f"/worker/{worker_id}/input_mode", {"input_mode": "hijack"})
        assert status2 == 200, f"input_mode revert: {body2}"
        assert body2.get("input_mode") == "hijack"
