"""Full-stack E2E tests: HostedSessionRuntime + CF proxy + hibernation.

Tests the complete Python-library-to-CF-DO proxy chain:
  HostedSessionRuntime (shell connector) ↔ worker WS → CF DO → browser WS

Run with:
    E2E=1 uv run pytest tests/test_e2e_full_stack.py -v
    REAL_CF=1 REAL_CF_URL=https://...workers.dev uv run pytest tests/test_e2e_full_stack.py -v
    SLOW=1 uv run pytest tests/test_e2e_full_stack.py -v  # includes hibernation test
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import unittest.mock
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest
import websockets
from provide.terminal.control_channel import encode_control

_WS_TIMEOUT_S = 15.0
_HTTP_UA = "provide-uterm-e2e-test/1.0"
# In AUTH_MODE=dev, any non-empty bearer token is accepted for worker auth.
_DEV_BEARER = "e2e-dev-token"

# CF Access service token for real_cf tests.
_CF_CLIENT_ID = os.environ.get("CF_ACCESS_CLIENT_ID", "")
_CF_CLIENT_SECRET = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")
_WORKER_BEARER_TOKEN = os.environ.get("CF_WORKER_BEARER_TOKEN", "")


def _cf_access_headers(url: str = "") -> dict[str, str]:
    """Return CF Access headers only for real CF (https), not local pywrangler (http)."""
    if url.startswith("http://"):
        return {}
    if _CF_CLIENT_ID and _CF_CLIENT_SECRET:
        return {"CF-Access-Client-Id": _CF_CLIENT_ID, "CF-Access-Client-Secret": _CF_CLIENT_SECRET}
    return {}


# ---------------------------------------------------------------------------
# HTTP + WS helpers (duplicated from test_e2e_ws — no shared test package)
# ---------------------------------------------------------------------------


def _base_ws(base_http: str) -> str:
    return base_http.replace("http://", "ws://").replace("https://", "wss://")


def _ws_connect(uri: str):
    """Connect with CF Access headers when targeting real CF (wss://)."""
    extra = _cf_access_headers(uri)
    if "/ws/worker/" in uri and uri.startswith("wss://"):
        extra["Authorization"] = f"Bearer {_WORKER_BEARER_TOKEN or _DEV_BEARER}"
    return websockets.connect(uri, additional_headers=extra) if extra else websockets.connect(uri)


def _http_get(base: str, path: str) -> tuple[int, object]:
    url = f"{base}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": _HTTP_UA, **_cf_access_headers(url)})  # noqa: S310
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
        headers={"Content-Type": "application/json", "User-Agent": _HTTP_UA, **_cf_access_headers(url)},
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
    # Handle control-channel encoded frames (DLE+STX prefix)
    if raw and raw[0] == "\x10" and len(raw) > 11 and raw[1] == "\x02":
        payload_start = 11  # skip DLE + STX + 8-hex-length + ':'
        return json.loads(raw[payload_start:])
    return json.loads(raw)


async def _drain_until(ws, target_type: str, max_frames: int = 10, timeout: float = _WS_TIMEOUT_S) -> dict | None:
    for _ in range(max_frames):
        frame = await _recv_ws(ws, timeout=timeout)
        if frame.get("type") == target_type:
            return frame
    return None


# ---------------------------------------------------------------------------
# CF Access header injection for HostedSessionRuntime outbound WS
# ---------------------------------------------------------------------------

_real_ws_connect = websockets.connect


def _patched_ws_connect(uri, **kwargs):
    """Wrap websockets.connect to inject CF Access headers into outbound connections."""
    extra = _cf_access_headers(str(uri))
    if extra:
        existing = dict(kwargs.get("additional_headers", {}) or {})
        existing.update(extra)
        kwargs["additional_headers"] = existing
    return _real_ws_connect(uri, **kwargs)


@pytest.fixture(autouse=True)
def _inject_cf_headers_into_runtime(request: pytest.FixtureRequest):
    """Patch websockets.connect for real_cf tests so HostedSessionRuntime sends CF Access headers."""
    if not request.node.get_closest_marker("real_cf"):
        yield  # no patching for local pywrangler tests
        return
    with unittest.mock.patch("websockets.connect", side_effect=_patched_ws_connect):
        yield


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def shell_runtime(wrangler_server: str):
    """Start a HostedSessionRuntime with a shell connector against the test server.

    Yields ``(runtime, worker_id)``.  The runtime is stopped on teardown.
    """
    from provide.terminal.server.models import RecordingConfig, SessionDefinition
    from provide.terminal.server.runtime import HostedSessionRuntime

    worker_id = f"e2e-{uuid.uuid4().hex[:8]}"
    defn = SessionDefinition(
        session_id=worker_id,
        display_name="E2E Test Shell",
        connector_type="shell",
        connector_config={},
        input_mode="open",
    )
    recording = RecordingConfig(enabled_by_default=False, directory=Path("/tmp"), max_bytes=10_000_000)
    rt = HostedSessionRuntime(
        defn,
        public_base_url=wrangler_server,
        recording=recording,
        worker_bearer_token=_WORKER_BEARER_TOKEN or _DEV_BEARER,
    )
    await rt.start()
    await asyncio.sleep(2.0)  # allow WS connect + initial snapshot send
    yield rt, worker_id
    await rt.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_hosted_runtime_connects_and_appears_in_sessions(wrangler_server: str, shell_runtime) -> None:
    """HostedSessionRuntime.start() → runtime connected; session in GET /api/sessions."""
    rt, worker_id = shell_runtime
    status = rt.status()
    assert status.connected, f"runtime not connected; lifecycle={status.lifecycle_state} last_error={status.last_error}"
    assert status.lifecycle_state == "running"

    # Session should be registered in KV (local KV in pywrangler dev, real KV on CF).
    status_code, body = await asyncio.to_thread(_http_get, wrangler_server, "/api/sessions")
    assert status_code == 200, f"/api/sessions returned {status_code}: {body}"
    assert isinstance(body, list)
    session_ids = [s.get("session_id") for s in body]
    assert worker_id in session_ids, f"{worker_id} not found in sessions: {session_ids}"


@pytest.mark.e2e
async def test_hosted_runtime_snapshot_reaches_browser(wrangler_server: str) -> None:
    """Browser connected before runtime receives the initial snapshot broadcast from the shell connector."""
    from provide.terminal.server.models import RecordingConfig, SessionDefinition
    from provide.terminal.server.runtime import HostedSessionRuntime

    worker_id = f"e2e-{uuid.uuid4().hex[:8]}"
    base_ws = _base_ws(wrangler_server)
    browser_uri = f"{base_ws}/ws/browser/{worker_id}/term"

    defn = SessionDefinition(
        session_id=worker_id,
        display_name="E2E Snapshot Test",
        connector_type="shell",
        connector_config={},
        input_mode="open",
    )
    recording = RecordingConfig(enabled_by_default=False, directory=Path("/tmp"), max_bytes=10_000_000)
    rt = HostedSessionRuntime(
        defn,
        public_base_url=wrangler_server,
        recording=recording,
        worker_bearer_token=_WORKER_BEARER_TOKEN or _DEV_BEARER,
    )
    try:
        async with _ws_connect(browser_uri) as browser_ws:
            # Drain hello frames sent by fetch() and webSocketOpen before runtime starts.
            await _drain_until(browser_ws, "hello", max_frames=5, timeout=5.0)
            # Start runtime — shell connector sends initial snapshot over worker WS;
            # the DO broadcasts it to all connected browsers (broadcast_worker_frame).
            await rt.start()
            # Drain frames: may include hijack_state, worker_connected before snapshot.
            received = await _drain_until(browser_ws, "snapshot", max_frames=15, timeout=15.0)
        assert received is not None, "browser did not receive a snapshot frame"
        assert isinstance(received.get("screen"), str), f"snapshot.screen not a string: {received}"
    finally:
        await rt.stop()


@pytest.mark.e2e
async def test_hosted_runtime_hijack_cycle(wrangler_server: str, shell_runtime) -> None:
    """Acquire hijack via REST, fetch snapshot, release — while HostedSessionRuntime is live."""
    rt, worker_id = shell_runtime
    assert rt.status().connected, f"runtime not connected; last_error={rt.status().last_error}"

    st1, b1 = await asyncio.to_thread(
        _http_post,
        wrangler_server,
        f"/worker/{worker_id}/hijack/acquire",
        {"owner": "e2e-full-stack", "lease_s": 30},
    )
    assert st1 == 200, f"acquire failed ({st1}): {b1}"
    hijack_id = b1["hijack_id"]

    try:
        # Shell connector sends an initial snapshot; the DO stores it in SQLite.
        st2, b2 = await asyncio.to_thread(
            _http_get, wrangler_server, f"/worker/{worker_id}/hijack/{hijack_id}/snapshot"
        )
        assert st2 == 200, f"snapshot endpoint ({st2}): {b2}"
        assert b2.get("ok") is True  # type: ignore[union-attr]
        snap = b2.get("snapshot")  # type: ignore[union-attr]
        assert snap is not None, "snapshot field missing from response"
        assert isinstance(snap.get("screen"), str), f"snapshot.screen is not a string: {snap}"
    finally:
        _http_post(wrangler_server, f"/worker/{worker_id}/hijack/{hijack_id}/release", {})


@pytest.mark.e2e
@pytest.mark.real_cf
async def test_two_browsers_receive_same_snapshot(wrangler_server: str) -> None:
    """Two simultaneous browser connections to the same DO both receive the worker's snapshot broadcast."""
    base_ws = _base_ws(wrangler_server)
    worker_id = f"e2e-{uuid.uuid4().hex[:8]}"
    worker_uri = f"{base_ws}/ws/worker/{worker_id}/term"
    browser_uri = f"{base_ws}/ws/browser/{worker_id}/term"
    snapshot_screen = f"dual-browser-{uuid.uuid4().hex[:6]}"

    async with (
        _ws_connect(worker_uri) as worker_ws,
        _ws_connect(browser_uri) as browser_a,
        _ws_connect(browser_uri) as browser_b,
    ):
        # Drain hello frames (fetch() + webSocketOpen) from both browsers.
        await _drain_until(browser_a, "hello", max_frames=5, timeout=5.0)
        await _drain_until(browser_b, "hello", max_frames=5, timeout=5.0)

        # Worker sends snapshot → DO calls broadcast_to_browsers → both sockets receive it.
        await worker_ws.send(encode_control({"type": "snapshot", "screen": snapshot_screen, "ts": time.time()}))

        snap_a, snap_b = await asyncio.gather(
            _drain_until(browser_a, "snapshot", max_frames=10, timeout=10.0),
            _drain_until(browser_b, "snapshot", max_frames=10, timeout=10.0),
        )

    assert snap_a is not None, "browser A did not receive snapshot"
    assert snap_b is not None, "browser B did not receive snapshot"
    assert snap_a.get("screen") == snapshot_screen, f"browser A screen mismatch: {snap_a}"
    assert snap_b.get("screen") == snapshot_screen, f"browser B screen mismatch: {snap_b}"


@pytest.mark.real_cf
@pytest.mark.slow
async def test_state_persists_after_do_hibernation(wrangler_server: str) -> None:
    """Snapshot written before DO hibernation survives and is readable after the DO wakes.

    Flow:
      1. Raw worker WS sends snapshot → DO stores in SQLite.
      2. Worker disconnects → DO marks offline.
      3. Wait 40s → DO hibernates (~30s idle on real CF).
      4. POST acquire wakes the DO; _restore_state() loads snapshot from SQLite.
      5. GET snapshot → assert screen matches.
    """
    base_ws = _base_ws(wrangler_server)
    worker_id = f"e2e-hib-{uuid.uuid4().hex[:8]}"
    worker_uri = f"{base_ws}/ws/worker/{worker_id}/term"
    snapshot_screen = f"persist-me-{uuid.uuid4().hex[:6]}"

    # Step 1-2: connect, send control-channel encoded snapshot, disconnect.
    async with _ws_connect(worker_uri) as ws:
        await ws.send(encode_control({"type": "snapshot", "screen": snapshot_screen, "ts": time.time()}))
        await asyncio.sleep(1.0)  # let DO process the frame before close

    # Step 3: wait for DO to hibernate.
    await asyncio.sleep(40.0)

    # Step 4: HTTP request wakes the DO; _restore_state() loads snapshot from SQLite.
    st1, b1 = await asyncio.to_thread(
        _http_post,
        wrangler_server,
        f"/worker/{worker_id}/hijack/acquire",
        {"owner": "post-hibernation", "lease_s": 30},
    )
    assert st1 == 200, f"acquire after hibernation failed ({st1}): {b1}"
    hijack_id = b1["hijack_id"]

    try:
        # Step 5: snapshot must survive the hibernation cycle.
        st2, b2 = await asyncio.to_thread(
            _http_get, wrangler_server, f"/worker/{worker_id}/hijack/{hijack_id}/snapshot"
        )
        assert st2 == 200, f"snapshot after hibernation ({st2}): {b2}"
        snap = b2.get("snapshot")  # type: ignore[union-attr]
        assert snap is not None, "snapshot missing after DO hibernation"
        assert snap.get("screen") == snapshot_screen, (
            f"snapshot screen mismatch after hibernation: expected={snapshot_screen!r} got={snap.get('screen')!r}"
        )
    finally:
        _http_post(wrangler_server, f"/worker/{worker_id}/hijack/{hijack_id}/release", {})
