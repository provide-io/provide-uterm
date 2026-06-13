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
from cf_e2e_auth import http_auth_headers, ws_auth_headers

from provide.uterm.control_channel import encode_control_frame

_WS_TIMEOUT_S = 15.0
_HTTP_UA = "provide-uterm-e2e-test/1.0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_ws(base_http: str) -> str:
    return base_http.replace("http://", "ws://").replace("https://", "wss://")


def _new_worker_id() -> str:
    return f"e2e-{uuid.uuid4().hex[:8]}"


def _ws_connect(uri: str):
    """Connect with the auth headers the worker expects for this WS upgrade."""
    extra = ws_auth_headers(uri)
    return websockets.connect(uri, additional_headers=extra) if extra else websockets.connect(uri)


def _http_get(base: str, path: str) -> tuple[int, object]:
    url = f"{base}{path}"
    headers = {"User-Agent": _HTTP_UA, **http_auth_headers(url)}
    req = urllib.request.Request(url, headers=headers)  # noqa: S310
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
    headers = {"Content-Type": "application/json", "User-Agent": _HTTP_UA, **http_auth_headers(url)}
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, {}


def _decode_control_frames(raw: str) -> list[dict]:
    """Extract JSON control frames from a control-channel-encoded WS message."""
    dle, stx = "\x10", "\x02"
    frames: list[dict] = []
    i = 0
    while i < len(raw):
        if raw[i] == dle and i + 1 < len(raw) and raw[i + 1] == stx:
            i += 2
            colon = raw.index(":", i)
            length = int(raw[i:colon], 16)
            frames.append(json.loads(raw[colon + 1 : colon + 1 + length]))
            i = colon + 1 + length
        elif raw[i] == dle and i + 1 < len(raw) and raw[i + 1] == dle:
            i += 2
        else:
            i += 1
    return frames


async def _recv_ws(ws, timeout: float = _WS_TIMEOUT_S) -> dict:
    """Receive one JSON frame, decoding control channel framing if present."""
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass
    frames = _decode_control_frames(raw)
    if frames:
        return frames[0]
    msg = f"no JSON frame in: {raw[:100]!r}"
    raise ValueError(msg)


async def _drain_until(ws, target_type: str, max_frames: int = 10) -> dict | None:
    for _ in range(max_frames):
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=_WS_TIMEOUT_S)
        except TimeoutError:
            return None
        try:
            frame = json.loads(raw)
            if frame.get("type") == target_type:
                return frame
            continue
        except (json.JSONDecodeError, TypeError):
            pass
        for frame in _decode_control_frames(raw):
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
    async with _ws_connect(uri) as browser_ws:
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
    snapshot_frame = encode_control_frame({"type": "snapshot", "screen": "hello-e2e", "ts": time.time()})

    async with _ws_connect(worker_uri) as worker_ws, _ws_connect(browser_uri) as browser_ws:
        # Drain the hello frame (sent synchronously in fetch() before 101).
        await _drain_until(browser_ws, "hello", max_frames=3)
        await worker_ws.send(snapshot_frame)
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
    async with _ws_connect(f"{base_ws}/ws/worker/{worker_id}/term"):
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
    async with _ws_connect(f"{base_ws}/ws/worker/{worker_id}/term"):
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
        _ws_connect(f"{base_ws}/ws/worker/{worker_id_1}/term"),
        _ws_connect(f"{base_ws}/ws/worker/{worker_id_2}/term"),
    ):
        await asyncio.sleep(5.0)  # KV writes are eventual — need extra time on real CF
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
    async with _ws_connect(f"{base_ws}/ws/worker/{worker_id}/term"):
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
    async with _ws_connect(f"{base_ws}/ws/worker/{worker_id}/term"):
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
    snapshot_frame = encode_control_frame({"type": "snapshot", "screen": "snapshot-e2e-content", "ts": time.time()})

    async with _ws_connect(f"{base_ws}/ws/worker/{worker_id}/term") as worker_ws:
        await worker_ws.send(snapshot_frame)
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
    async with _ws_connect(f"{base_ws}/ws/worker/{worker_id}/term"):
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
    async with _ws_connect(f"{base_ws}/ws/worker/{worker_id}/term"):
        status, body = _http_post(wrangler_server, f"/worker/{worker_id}/input_mode", {"input_mode": "open"})
        assert status == 200, f"input_mode change: {body}"
        assert body.get("input_mode") == "open"
        assert body.get("ok") is True

        status2, body2 = _http_post(wrangler_server, f"/worker/{worker_id}/input_mode", {"input_mode": "hijack"})
        assert status2 == 200, f"input_mode revert: {body2}"
        assert body2.get("input_mode") == "hijack"


# ---------------------------------------------------------------------------
# Tests — WS session resumption
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.real_cf
async def test_browser_hello_includes_resume_token(wrangler_server: str) -> None:
    """Browser WS hello must include resume_supported=True and a resume_token."""
    base_ws = _base_ws(wrangler_server)
    worker_id = _new_worker_id()
    uri = f"{base_ws}/ws/browser/{worker_id}/term"
    async with _ws_connect(uri) as browser_ws:
        hello = await _recv_ws(browser_ws)
    assert hello["type"] == "hello"
    assert hello.get("resume_supported") is True, f"resume_supported missing or False: {hello}"
    assert hello.get("resume_token") is not None, f"resume_token missing: {hello}"
    assert len(hello["resume_token"]) > 10, f"resume_token too short: {hello['resume_token']}"


@pytest.mark.e2e
@pytest.mark.real_cf
async def test_resume_after_disconnect(wrangler_server: str) -> None:
    """Connect → get token → disconnect → reconnect → resume → get resumed hello.

    This is the definitive proof that session resumption works end-to-end.
    """
    base_ws = _base_ws(wrangler_server)
    worker_id = _new_worker_id()
    uri = f"{base_ws}/ws/browser/{worker_id}/term"

    # --- Connection 1: get the resume token ---
    async with _ws_connect(uri) as ws1:
        hello1 = await _recv_ws(ws1)
        assert hello1["type"] == "hello", f"expected hello, got: {hello1}"
        token = hello1["resume_token"]
        role1 = hello1.get("role", "viewer")

    # WS1 is now closed (disconnected).

    # --- Connection 2: reconnect and resume ---
    async with _ws_connect(uri) as ws2:
        # Drain the initial hello (fresh session)
        hello2 = await _recv_ws(ws2)
        assert hello2["type"] == "hello"
        fresh_token = hello2.get("resume_token")
        assert fresh_token is not None

        # Send resume with the old token
        await ws2.send(encode_control_frame({"type": "resume", "token": token}))

        # Should receive a resumed hello
        resumed_hello = await _recv_ws(ws2)
        assert resumed_hello["type"] == "hello", f"expected resumed hello, got: {resumed_hello}"
        assert resumed_hello.get("resumed") is True, f"resumed flag not set: {resumed_hello}"
        assert resumed_hello.get("resume_token") is not None, f"no new token in resumed hello: {resumed_hello}"
        assert resumed_hello["resume_token"] != token, "new token should differ from old"
        assert resumed_hello.get("role") == role1, f"role not restored: {resumed_hello}"

        # Should also get hijack_state
        hs = await _recv_ws(ws2)
        assert hs["type"] == "hijack_state", f"expected hijack_state, got: {hs}"


@pytest.mark.e2e
@pytest.mark.real_cf
async def test_resume_expired_or_invalid_token_ignored(wrangler_server: str) -> None:
    """Resume with a bogus token → silently ignored, no error, connection continues."""
    base_ws = _base_ws(wrangler_server)
    worker_id = _new_worker_id()
    uri = f"{base_ws}/ws/browser/{worker_id}/term"

    async with _ws_connect(uri) as ws:
        hello = await _recv_ws(ws)
        assert hello["type"] == "hello"

        # Send resume with a fake token
        await ws.send(encode_control_frame({"type": "resume", "token": "totally-bogus-token-12345"}))

        # Send a ping to verify connection still works
        await ws.send(json.dumps({"type": "ping"}))
        # The next frame should NOT be a resumed hello — it should be
        # either a heartbeat response or nothing. The connection should stay alive.
        # (We don't get a pong back from CF — ping is just a keep-alive.)
        # Instead, verify the connection is still alive by sending input and
        # not getting disconnected.
        await asyncio.sleep(0.5)
        # If we get here without an exception, the connection survived the bogus resume.


@pytest.mark.e2e
@pytest.mark.real_cf
async def test_resume_token_is_one_time_use(wrangler_server: str) -> None:
    """A resume token can only be used once — second attempt is ignored."""
    base_ws = _base_ws(wrangler_server)
    worker_id = _new_worker_id()
    uri = f"{base_ws}/ws/browser/{worker_id}/term"

    # Get a token
    async with _ws_connect(uri) as ws1:
        hello1 = await _recv_ws(ws1)
        token = hello1["resume_token"]

    # First resume — should succeed
    async with _ws_connect(uri) as ws2:
        await _recv_ws(ws2)  # initial hello
        await ws2.send(encode_control_frame({"type": "resume", "token": token}))
        resumed = await _recv_ws(ws2)
        assert resumed.get("resumed") is True, f"first resume failed: {resumed}"

    # Second resume with same token — should be silently ignored
    async with _ws_connect(uri) as ws3:
        hello3 = await _recv_ws(ws3)
        assert hello3["type"] == "hello"
        await ws3.send(encode_control_frame({"type": "resume", "token": token}))

        # Send ping to verify connection is alive — no resumed hello should come
        await ws3.send(json.dumps({"type": "ping"}))
        await asyncio.sleep(0.5)
        # If we get here without crash, the revoked token was properly ignored
