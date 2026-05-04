#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""E2E tests for recording/replay routes against a live pywrangler dev server.

Run with:
    uv run pytest -m e2e packages/provide-uterm-cloudflare/tests/test_e2e_recording.py
or:
    E2E=1 uv run pytest packages/provide-uterm-cloudflare/tests/test_e2e_recording.py

Proves:
  1. GET /recording returns metadata with enabled=True
  2. After worker sends snapshots, /recording/entries returns them as {ts, event, data}
  3. ?event=snapshot filters correctly
  4. ?limit=1 respects pagination
  5. Status item shows recording_available=True after events
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

from provide.terminal.control_channel import encode_control

_WS_TIMEOUT_S = 0.5
_WS_PROCESS_S = 1.0
_HTTP_UA = "provide-uterm-e2e-recording/1.0"

# CF Access service token for real_cf tests (bypasses Cloudflare Access login).
# Set via env vars or fall back to empty (local pywrangler dev uses AUTH_MODE=dev).
_CF_CLIENT_ID = os.environ.get("CF_ACCESS_CLIENT_ID", "")
_CF_CLIENT_SECRET = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")
_WORKER_BEARER_TOKEN = os.environ.get("CF_WORKER_BEARER_TOKEN", "")


def _cf_access_headers(url: str = "") -> dict[str, str]:
    """Return CF Access service token headers when targeting real CF (https)."""
    if url.startswith("http://"):
        return {}  # local pywrangler — no CF Access needed
    if _CF_CLIENT_ID and _CF_CLIENT_SECRET:
        return {"CF-Access-Client-Id": _CF_CLIENT_ID, "CF-Access-Client-Secret": _CF_CLIENT_SECRET}
    return {}


def _base_ws(base_http: str) -> str:
    return base_http.replace("http://", "ws://").replace("https://", "wss://")


def _new_worker_id() -> str:
    return f"e2e-rec-{uuid.uuid4().hex[:8]}"


def _http_get_json(base: str, path: str) -> tuple[int, object]:
    url = f"{base}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": _HTTP_UA, **_cf_access_headers(url)})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        with contextlib.suppress(Exception):
            return exc.code, json.loads(exc.read())
        return exc.code, {}


def _ws_connect(uri: str):
    """Connect with CF Access headers when targeting real CF (wss://)."""
    extra = _cf_access_headers(uri)
    if _WORKER_BEARER_TOKEN and "/ws/worker/" in uri and uri.startswith("wss://"):
        extra["Authorization"] = f"Bearer {_WORKER_BEARER_TOKEN}"
    return websockets.connect(uri, additional_headers=extra) if extra else websockets.connect(uri)


async def _connect_worker_send_snapshots(
    base_ws: str,
    worker_id: str,
    screens: list[str],
) -> None:
    """Connect worker WS, drain hello, send snapshot frames, disconnect."""
    uri = f"{base_ws}/ws/worker/{worker_id}/term"
    async with _ws_connect(uri) as ws:
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(ws.recv(), timeout=_WS_TIMEOUT_S)
        for screen in screens:
            frame = encode_control(
                {
                    "type": "snapshot",
                    "screen": screen,
                    "cursor": {"x": 0, "y": 0},
                    "cols": 80,
                    "rows": 25,
                    "screen_hash": f"rec-{hash(screen) & 0xFFFF:04x}",
                    "cursor_at_end": True,
                    "has_trailing_space": False,
                    "prompt_detected": {"prompt_id": "test"},
                    "ts": time.time(),
                }
            )
            await ws.send(frame)
            await asyncio.sleep(0.3)
        await asyncio.sleep(_WS_PROCESS_S)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_recording_meta_empty_session(wrangler_server: str) -> None:
    """GET /recording for a fresh session returns enabled=True, exists=False."""
    wid = _new_worker_id()
    status, body = _http_get_json(wrangler_server, f"/api/sessions/{wid}/recording")
    assert status == 200
    assert body["enabled"] is True
    assert body["exists"] is False
    assert body["entry_count"] == 0


@pytest.mark.e2e
def test_recording_entries_after_worker_snapshots(wrangler_server: str) -> None:
    """After worker sends snapshots, /recording/entries returns them as {ts, event, data}."""
    wid = _new_worker_id()
    ws_base = _base_ws(wrangler_server)

    asyncio.get_event_loop().run_until_complete(
        _connect_worker_send_snapshots(ws_base, wid, ["$ ls", "$ pwd", "$ whoami"])
    )

    # Check meta
    status, meta = _http_get_json(wrangler_server, f"/api/sessions/{wid}/recording")
    assert status == 200
    assert meta["enabled"] is True
    assert meta["entry_count"] >= 3  # at least 3 snapshots (may include worker_connected too)
    assert meta["exists"] is True

    # Check entries
    status, entries = _http_get_json(wrangler_server, f"/api/sessions/{wid}/recording/entries")
    assert status == 200
    assert isinstance(entries, list)
    assert len(entries) >= 3

    # Verify {ts, event, data} shape
    for entry in entries:
        assert "ts" in entry
        assert "event" in entry
        assert "data" in entry
        assert isinstance(entry["ts"], (int, float))

    # At least one snapshot with screen data
    snapshots = [e for e in entries if e["event"] == "snapshot"]
    assert len(snapshots) >= 3
    assert "screen" in snapshots[0]["data"]


@pytest.mark.e2e
def test_recording_entries_event_filter(wrangler_server: str) -> None:
    """?event=snapshot filters to only snapshot entries."""
    wid = _new_worker_id()
    ws_base = _base_ws(wrangler_server)

    asyncio.get_event_loop().run_until_complete(_connect_worker_send_snapshots(ws_base, wid, ["$ hello"]))

    status, entries = _http_get_json(wrangler_server, f"/api/sessions/{wid}/recording/entries?event=snapshot")
    assert status == 200
    assert isinstance(entries, list)
    assert all(e["event"] == "snapshot" for e in entries)


@pytest.mark.e2e
def test_recording_entries_limit(wrangler_server: str) -> None:
    """?limit=1 respects pagination."""
    wid = _new_worker_id()
    ws_base = _base_ws(wrangler_server)

    asyncio.get_event_loop().run_until_complete(_connect_worker_send_snapshots(ws_base, wid, ["$ a", "$ b", "$ c"]))

    status, entries = _http_get_json(wrangler_server, f"/api/sessions/{wid}/recording/entries?limit=1")
    assert status == 200
    assert len(entries) == 1


@pytest.mark.e2e
def test_status_item_shows_recording_available(wrangler_server: str) -> None:
    """Session status shows recording_available=True after events are stored."""
    wid = _new_worker_id()
    ws_base = _base_ws(wrangler_server)

    asyncio.get_event_loop().run_until_complete(_connect_worker_send_snapshots(ws_base, wid, ["$ test"]))

    status, body = _http_get_json(wrangler_server, f"/api/sessions/{wid}")
    assert status == 200
    assert body["recording_enabled"] is True
    assert body["recording_available"] is True
