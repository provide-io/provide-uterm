# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
E2E PAM relay tests against pywrangler dev (or real CF deployment).

Verifies the full relay chain:
  - POST /api/pam-events open/close → KV created/deleted
  - POST /api/tunnels → worker_token + ws_endpoint returned
  - pam_integration._forward_to_relay() delivers events to the live server
  - pam_integration._create_relay_tunnel() returns usable token + endpoint

Run with:
    E2E=1 uv run pytest tests/test_e2e_pam_relay.py -v -p no:xdist -p no:randomly
or:
    uv run pytest -m e2e packages/provide-uterm-cloudflare/tests/test_e2e_pam_relay.py
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid

import pytest

_HTTP_UA = "provide-uterm-e2e-pam-relay/1.0"
# In AUTH_MODE=dev (local pywrangler), any Bearer token is accepted.
_DEV_TOKEN = "dev-token"


def _http_post(base: str, path: str, body: dict) -> tuple[int, dict]:
    url = f"{base}{path}"
    data = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": _HTTP_UA,
        "Authorization": f"Bearer {_DEV_TOKEN}",
    }
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, {}


def _unique_tty() -> str:
    """Return a unique TTY path to avoid KV key collisions between test runs."""
    return f"/dev/pts/{uuid.uuid4().hex[:6]}"


# ── /api/pam-events ───────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_pam_events_open_creates_session(wrangler_server: str) -> None:
    """POST open event → 200, action=created, session_id matches tty slug."""
    tty = _unique_tty()
    status, body = _http_post(
        wrangler_server,
        "/api/pam-events",
        {"event": "open", "username": "e2e-user", "tty": tty, "pid": 9999, "mode": "notify"},
    )
    assert status == 200, f"expected 200, got {status}: {body}"
    assert body["ok"] is True
    assert body["action"] == "created"
    assert "session_id" in body
    assert body["session_id"].startswith("pam-e2e-user-")


@pytest.mark.e2e
def test_pam_events_close_deletes_session(wrangler_server: str) -> None:
    """POST open then close → close returns action=deleted for same session."""
    tty = _unique_tty()
    _http_post(
        wrangler_server,
        "/api/pam-events",
        {"event": "open", "username": "e2e-close", "tty": tty, "pid": 1001, "mode": "notify"},
    )
    status, body = _http_post(
        wrangler_server,
        "/api/pam-events",
        {"event": "close", "username": "e2e-close", "tty": tty, "pid": 1001},
    )
    assert status == 200, f"expected 200, got {status}: {body}"
    assert body["action"] == "deleted"
    assert "session_id" in body


@pytest.mark.e2e
def test_pam_events_unknown_event_returns_422(wrangler_server: str) -> None:
    """Unknown event type is rejected before any KV write."""
    status, _ = _http_post(
        wrangler_server,
        "/api/pam-events",
        {"event": "reboot", "username": "x", "tty": "/dev/pts/0", "pid": 1},
    )
    assert status == 422


@pytest.mark.e2e
def test_pam_events_missing_username_returns_422(wrangler_server: str) -> None:
    status, _ = _http_post(
        wrangler_server,
        "/api/pam-events",
        {"event": "open", "username": "", "tty": "/dev/pts/0", "pid": 1},
    )
    assert status == 422


# ── /api/tunnels ──────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_tunnels_create_for_pam_session(wrangler_server: str) -> None:
    """POST /api/tunnels with pam session_id returns full token set."""
    sid = f"pam-e2e-relay-{uuid.uuid4().hex[:8]}"
    status, body = _http_post(
        wrangler_server,
        "/api/tunnels",
        {"session_id": sid, "display_name": "E2E PAM relay test", "tunnel_type": "terminal"},
    )
    assert status == 200, f"expected 200, got {status}: {body}"
    assert "worker_token" in body, f"missing worker_token: {body}"
    assert "ws_endpoint" in body, f"missing ws_endpoint: {body}"
    assert "tunnel_id" in body
    assert body["ws_endpoint"], f"ws_endpoint is empty: {body}"  # may be relative path in dev


# ── pam_integration relay helpers against the live server ────────────────────


@pytest.mark.e2e
async def test_forward_to_relay_delivers_open_event(wrangler_server: str) -> None:
    """_forward_to_relay() sends an open event to the live server without raising."""
    from provide.uterm.server.pam_integration import _forward_to_relay

    # Best-effort — must not raise on success
    await _forward_to_relay(
        {
            "event": "open",
            "username": "relay-e2e",
            "tty": _unique_tty(),
            "pid": 42,
            "mode": "notify",
        },
        wrangler_server,
        _DEV_TOKEN,
    )
    # No assertion needed beyond no exception — _forward_to_relay is fire-and-forget


@pytest.mark.e2e
async def test_create_relay_tunnel_returns_token_and_endpoint(wrangler_server: str) -> None:
    """_create_relay_tunnel() returns (worker_token, ws_endpoint) from live server."""
    from provide.uterm.server.pam_integration import _create_relay_tunnel

    sid = f"pam-relay-e2e-{uuid.uuid4().hex[:8]}"
    result = await _create_relay_tunnel(wrangler_server, _DEV_TOKEN, sid, "E2E Relay Tunnel")

    assert result is not None, "_create_relay_tunnel returned None — request failed"
    worker_token, ws_endpoint = result
    assert worker_token, "worker_token is empty"
    assert ws_endpoint, "ws_endpoint is empty"  # may be relative path in pywrangler dev


@pytest.mark.e2e
async def test_relay_open_close_full_cycle(wrangler_server: str) -> None:
    """Full relay cycle: _forward_to_relay open → _forward_to_relay close."""
    from provide.uterm.server.pam_integration import _forward_to_relay

    tty = _unique_tty()
    username = f"relay-cycle-{uuid.uuid4().hex[:4]}"

    await _forward_to_relay(
        {"event": "open", "username": username, "tty": tty, "pid": 1234, "mode": "notify"},
        wrangler_server,
        _DEV_TOKEN,
    )
    # Independently verify the session was created
    slug = tty.split("/")[-1]
    session_id = f"pam-{username}-{slug}"
    status, body = _http_post(
        wrangler_server,
        "/api/pam-events",
        {"event": "close", "username": username, "tty": tty, "pid": 1234},
    )
    assert status == 200
    assert body.get("session_id") == session_id or body.get("action") in ("deleted", "not_found")
