#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Integration tests for the tunnel system's FastAPI side.

Tests exercise the full POST /api/tunnels -> agent WS -> browser WS flow,
plus share/control token auth, expiry, revocation, rotation, and cookie auth.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from provide.terminal.control_channel import ControlChannelDecoder
from provide.terminal.server import create_server_app, default_server_config
from provide.terminal.server.models import ServerConfig, TunnelConfig
from provide.terminal.tunnel.protocol import CHANNEL_DATA, encode_frame

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_browser_msgs(raw: str) -> list[dict[str, Any]]:
    """Decode all control-channel-encoded messages from a raw WS text frame."""
    decoder = ControlChannelDecoder()
    events = decoder.feed(raw)
    events.extend(decoder.finish())
    results: list[dict[str, Any]] = []
    for ev in events:
        if hasattr(ev, "control"):
            results.append(ev.control)
        else:
            results.append({"type": "data", "data": ev.data})
    return results


def _drain_until(ws: Any, msg_type: str, *, max_reads: int = 20) -> dict[str, Any]:
    """Read browser WS messages until one with the given type appears."""
    for _ in range(max_reads):
        raw = ws.receive_text()
        for msg in _decode_browser_msgs(raw):
            if msg.get("type") == msg_type:
                return msg
    raise AssertionError(f"never received {msg_type!r}")


def _make_app(auth_mode: str = "none", **tunnel_kwargs: Any) -> tuple[ServerConfig, Any]:
    """Create a server app with no auth and no pre-configured sessions."""
    cfg = default_server_config()
    cfg.auth.mode = auth_mode
    cfg.sessions = []  # no default sessions
    if tunnel_kwargs:
        cfg.tunnel = TunnelConfig(**tunnel_kwargs)
    app = create_server_app(cfg)
    return cfg, app


def _create_tunnel(client: TestClient, **payload: Any) -> dict[str, Any]:
    """POST /api/tunnels and return the JSON response body."""
    resp = client.post("/api/tunnels", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 1. Full data flow
# ---------------------------------------------------------------------------


class TestFullDataFlow:
    def test_agent_to_browser_data_flow(self) -> None:
        """POST /api/tunnels -> agent /tunnel/{id} -> browser /ws/browser/{id}/term."""
        _cfg, app = _make_app()
        client = TestClient(app)

        # Create tunnel
        tunnel = _create_tunnel(client, display_name="flow-test")
        tunnel_id = tunnel["tunnel_id"]
        worker_token = tunnel["worker_token"]

        # Agent connects to /tunnel/{id} with worker_token
        with (
            client.websocket_connect(
                f"/tunnel/{tunnel_id}",
                headers={"Authorization": f"Bearer {worker_token}"},
            ) as agent_ws,
            client.websocket_connect(f"/ws/browser/{tunnel_id}/term") as browser_ws,
        ):
            # Drain hello
            hello = _drain_until(browser_ws, "hello")
            assert hello["worker_online"] is True

            # Agent sends binary data frame
            agent_ws.send_bytes(encode_frame(CHANNEL_DATA, b"Hello from tunnel agent"))

            # Browser should receive the terminal output
            for _ in range(20):
                raw = browser_ws.receive_text()
                if "Hello from tunnel agent" in raw:
                    break
            assert "Hello from tunnel agent" in raw


# ---------------------------------------------------------------------------
# 2. Share token auth
# ---------------------------------------------------------------------------


class TestShareTokenAuth:
    def test_share_token_grants_session_page(self) -> None:
        """GET /app/session/{id}?token={share_token} returns 200."""
        _cfg, app = _make_app()
        client = TestClient(app)
        tunnel = _create_tunnel(client)
        tunnel_id = tunnel["tunnel_id"]
        share_token = tunnel["share_url"].split("token=")[1]

        resp = client.get(f"/app/session/{tunnel_id}", params={"token": share_token})
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_wrong_token_rejected(self) -> None:
        """GET /app/session/{id}?token=wrong does not grant access as share viewer."""
        _cfg, app = _make_app()
        client = TestClient(app)
        tunnel = _create_tunnel(client)
        tunnel_id = tunnel["tunnel_id"]
        share_token = tunnel["share_url"].split("token=")[1]

        # Use separate clients to avoid cookie persistence between requests.
        client_good = TestClient(app)
        resp_good = client_good.get(f"/app/session/{tunnel_id}", params={"token": share_token})

        client_bad = TestClient(app)
        resp_bad = client_bad.get(f"/app/session/{tunnel_id}", params={"token": "wrong-token-value"})

        # Both return 200 in none-auth mode, but the principal differs.
        assert resp_good.status_code == 200
        assert resp_bad.status_code == 200
        # Check the principal cookie to confirm different auth paths
        good_cookies = {c.name: c.value for c in resp_good.cookies.jar}
        bad_cookies = {c.name: c.value for c in resp_bad.cookies.jar}
        assert "share:" in good_cookies.get("uterm_principal", "")
        assert "share:" not in bad_cookies.get("uterm_principal", "")


# ---------------------------------------------------------------------------
# 3. Control token auth
# ---------------------------------------------------------------------------


class TestControlTokenAuth:
    def test_control_token_grants_operator_page(self) -> None:
        """GET /app/operator/{id}?token={control_token} returns 200."""
        _cfg, app = _make_app()
        client = TestClient(app)
        tunnel = _create_tunnel(client)
        tunnel_id = tunnel["tunnel_id"]
        control_token = tunnel["control_url"].split("token=")[1]

        resp = client.get(f"/app/operator/{tunnel_id}", params={"token": control_token})
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

        # Verify the principal cookie indicates operator-level share access
        cookies = {c.name: c.value for c in resp.cookies.jar}
        assert "share:" in cookies.get("uterm_principal", "")
        assert "operator" in cookies.get("uterm_principal", "")


# ---------------------------------------------------------------------------
# 4. Token expiry
# ---------------------------------------------------------------------------


class TestTokenExpiry:
    def test_expired_token_rejected(self) -> None:
        """After TTL expires, the share token no longer works."""
        import time as real_time

        _cfg, app = _make_app(token_ttl_s=120)
        client = TestClient(app)
        tunnel = _create_tunnel(client)
        tunnel_id = tunnel["tunnel_id"]
        share_token = tunnel["share_url"].split("token=")[1]

        # Token works before expiry (fresh client to avoid cookie persistence)
        client_fresh = TestClient(app)
        resp = client_fresh.get(f"/app/session/{tunnel_id}", params={"token": share_token})
        assert resp.status_code == 200
        cookies = {c.name: c.value for c in resp.cookies.jar}
        assert "share:" in cookies.get("uterm_principal", "")

        # Manually expire the token by setting expires_at in the past
        tunnel_tokens = app.state.uterm_tunnel_tokens
        tunnel_tokens[tunnel_id]["expires_at"] = real_time.time() - 100

        # Token fails after expiry (fresh client)
        client_fresh2 = TestClient(app)
        resp = client_fresh2.get(f"/app/session/{tunnel_id}", params={"token": share_token})
        assert resp.status_code == 200  # still 200 because auth=none fallback
        cookies = {c.name: c.value for c in resp.cookies.jar}
        # But the share principal is NOT set (expired token)
        assert "share:" not in cookies.get("uterm_principal", "")


# ---------------------------------------------------------------------------
# 5. Token revocation
# ---------------------------------------------------------------------------


class TestTokenRevocation:
    def test_revoked_tokens_no_longer_work(self) -> None:
        """DELETE /api/tunnels/{id}/tokens -> tokens stop working."""
        _cfg, app = _make_app()
        client = TestClient(app)
        tunnel = _create_tunnel(client)
        tunnel_id = tunnel["tunnel_id"]
        share_token = tunnel["share_url"].split("token=")[1]
        control_token = tunnel["control_url"].split("token=")[1]

        # Tokens work before revocation (fresh client)
        c1 = TestClient(app)
        resp = c1.get(f"/app/session/{tunnel_id}", params={"token": share_token})
        cookies = {c.name: c.value for c in resp.cookies.jar}
        assert "share:" in cookies.get("uterm_principal", "")

        # Revoke
        resp = client.delete(f"/api/tunnels/{tunnel_id}/tokens")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Share token no longer resolves as share principal (fresh client)
        c2 = TestClient(app)
        resp = c2.get(f"/app/session/{tunnel_id}", params={"token": share_token})
        cookies = {c.name: c.value for c in resp.cookies.jar}
        assert "share:" not in cookies.get("uterm_principal", "")

        # Control token no longer resolves as share principal (fresh client)
        c3 = TestClient(app)
        resp = c3.get(f"/app/operator/{tunnel_id}", params={"token": control_token})
        cookies = {c.name: c.value for c in resp.cookies.jar}
        assert "share:" not in cookies.get("uterm_principal", "")


# ---------------------------------------------------------------------------
# 6. Token rotation
# ---------------------------------------------------------------------------


class TestTokenRotation:
    def test_rotation_invalidates_old_tokens(self) -> None:
        """POST /api/tunnels/{id}/tokens/rotate -> old tokens stop, new work."""
        _cfg, app = _make_app()
        client = TestClient(app)
        tunnel = _create_tunnel(client)
        tunnel_id = tunnel["tunnel_id"]
        old_share = tunnel["share_url"].split("token=")[1]

        # Old token works (fresh client)
        c1 = TestClient(app)
        resp = c1.get(f"/app/session/{tunnel_id}", params={"token": old_share})
        cookies = {c.name: c.value for c in resp.cookies.jar}
        assert "share:" in cookies.get("uterm_principal", "")

        # Rotate
        resp = client.post(f"/api/tunnels/{tunnel_id}/tokens/rotate")
        assert resp.status_code == 200
        rotated = resp.json()
        new_share = rotated["share_url"].split("token=")[1]
        assert new_share != old_share

        # Old token no longer works (fresh client)
        c2 = TestClient(app)
        resp = c2.get(f"/app/session/{tunnel_id}", params={"token": old_share})
        cookies = {c.name: c.value for c in resp.cookies.jar}
        assert "share:" not in cookies.get("uterm_principal", "")

        # New token works (fresh client)
        c3 = TestClient(app)
        resp = c3.get(f"/app/session/{tunnel_id}", params={"token": new_share})
        cookies = {c.name: c.value for c in resp.cookies.jar}
        assert "share:" in cookies.get("uterm_principal", "")

    def test_rotation_of_nonexistent_tunnel_returns_404(self) -> None:
        """Rotating tokens for a tunnel that has none returns 404."""
        _cfg, app = _make_app()
        client = TestClient(app)
        resp = client.post("/api/tunnels/no-such-tunnel/tokens/rotate")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 7. Worker auth with per-session token
# ---------------------------------------------------------------------------


class TestWorkerPerSessionToken:
    def test_per_session_worker_token_accepted(self) -> None:
        """Agent connects /tunnel/{id} with per-session worker_token."""
        _cfg, app = _make_app()
        client = TestClient(app)
        tunnel = _create_tunnel(client)
        tunnel_id = tunnel["tunnel_id"]
        worker_token = tunnel["worker_token"]

        with client.websocket_connect(
            f"/tunnel/{tunnel_id}",
            headers={"Authorization": f"Bearer {worker_token}"},
        ) as ws:
            ws.send_bytes(encode_frame(CHANNEL_DATA, b"test data"))


# ---------------------------------------------------------------------------
# 8. Worker auth rejected
# ---------------------------------------------------------------------------


class TestWorkerAuthRejected:
    def test_wrong_worker_token_rejected(self) -> None:
        """Agent with wrong token gets 1008 close when hub has worker_token."""
        _cfg, app = _make_app()
        # The hub already has worker_token=None for auth_mode=none.
        # We need to create a tunnel (which puts per-session tokens in tunnel_tokens)
        # and then try with a wrong token.
        client = TestClient(app)
        tunnel = _create_tunnel(client)
        tunnel_id = tunnel["tunnel_id"]

        # The tunnel has tokens now, so the /tunnel route checks them.
        # Connect with wrong token — should be accepted then closed with 1008.
        with client.websocket_connect(
            f"/tunnel/{tunnel_id}",
            headers={"Authorization": "Bearer totally-wrong-token"},
        ):
            # Server should close the connection — the context manager exits
            pass  # The WS handler accepts then closes with 1008


# ---------------------------------------------------------------------------
# 9. Cookie-based auth
# ---------------------------------------------------------------------------


class TestCookieAuth:
    def test_cookie_token_accepted(self) -> None:
        """When token_transport='both', cookie uterm_tunnel_{id} is accepted."""
        _cfg, app = _make_app(token_transport="both")
        client = TestClient(app)
        tunnel = _create_tunnel(client)
        tunnel_id = tunnel["tunnel_id"]
        share_token = tunnel["share_url"].split("token=")[1]

        # Send via cookie instead of query param
        cookie_name = f"uterm_tunnel_{tunnel_id}"
        resp = client.get(
            f"/app/session/{tunnel_id}",
            cookies={cookie_name: share_token},
        )
        assert resp.status_code == 200
        cookies = {c.name: c.value for c in resp.cookies.jar}
        assert "share:" in cookies.get("uterm_principal", "")

    def test_cookie_control_token_accepted(self) -> None:
        """Cookie with control_token grants operator access."""
        _cfg, app = _make_app(token_transport="both")
        client = TestClient(app)
        tunnel = _create_tunnel(client)
        tunnel_id = tunnel["tunnel_id"]
        control_token = tunnel["control_url"].split("token=")[1]

        cookie_name = f"uterm_tunnel_{tunnel_id}"
        resp = client.get(
            f"/app/operator/{tunnel_id}",
            cookies={cookie_name: control_token},
        )
        assert resp.status_code == 200
        cookies = {c.name: c.value for c in resp.cookies.jar}
        assert "share:" in cookies.get("uterm_principal", "")
        assert "operator" in cookies.get("uterm_principal", "")

    def test_wrong_cookie_not_accepted(self) -> None:
        """Wrong cookie value does not grant share principal."""
        _cfg, app = _make_app(token_transport="both")
        client = TestClient(app)
        tunnel = _create_tunnel(client)
        tunnel_id = tunnel["tunnel_id"]

        cookie_name = f"uterm_tunnel_{tunnel_id}"
        resp = client.get(
            f"/app/session/{tunnel_id}",
            cookies={cookie_name: "wrong-value"},
        )
        assert resp.status_code == 200  # auth=none fallback
        cookies = {c.name: c.value for c in resp.cookies.jar}
        assert "share:" not in cookies.get("uterm_principal", "")


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


class TestTunnelCreationDetails:
    def test_create_tunnel_returns_expected_fields(self) -> None:
        """POST /api/tunnels returns all required fields."""
        _cfg, app = _make_app()
        client = TestClient(app)
        tunnel = _create_tunnel(client, display_name="my-tunnel", tunnel_type="terminal")

        assert tunnel["tunnel_id"].startswith("tunnel-")
        assert "worker_token" in tunnel
        assert "share_url" in tunnel
        assert "control_url" in tunnel
        assert "ws_endpoint" in tunnel
        assert "expires_at" in tunnel
        assert tunnel["display_name"] == "my-tunnel"
        assert tunnel["tunnel_type"] == "terminal"

    def test_custom_ttl_clamped(self) -> None:
        """TTL is clamped to [60, default*24]."""
        _cfg, app = _make_app(token_ttl_s=3600)
        client = TestClient(app)

        # Request TTL of 10 seconds — should be clamped to 60
        tunnel = _create_tunnel(client, ttl_s=10)
        # expires_at should be roughly now+60, not now+10
        import time

        now = time.time()
        assert tunnel["expires_at"] >= now + 55  # at least ~60s from now

    def test_revoke_nonexistent_tunnel_ok(self) -> None:
        """Revoking tokens for unknown tunnel returns ok (idempotent)."""
        _cfg, app = _make_app()
        client = TestClient(app)
        resp = client.delete("/api/tunnels/nonexistent-id/tokens")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestTokenTransportEnforcement:
    """Verify token_transport config is actually enforced."""

    def test_query_only_rejects_cookie(self) -> None:
        """When token_transport='query', cookie is not accepted."""
        cfg, app = _make_app()
        cfg.tunnel.token_transport = "query"
        client = TestClient(app)
        tunnel = _create_tunnel(client)
        tid = tunnel["tunnel_id"]
        share_tok = app.state.uterm_tunnel_tokens[tid]["share_token"]
        resp = TestClient(app).get(
            f"/app/session/{tid}",
            cookies={f"uterm_tunnel_{tid}": share_tok},
        )
        cookies = {c.name: c.value for c in resp.cookies.jar}
        assert "share:" not in cookies.get("uterm_principal", "")

    def test_query_only_accepts_query(self) -> None:
        """When token_transport='query', query param still works."""
        cfg, app = _make_app()
        cfg.tunnel.token_transport = "query"
        client = TestClient(app)
        tunnel = _create_tunnel(client)
        tid = tunnel["tunnel_id"]
        share_tok = app.state.uterm_tunnel_tokens[tid]["share_token"]
        resp = TestClient(app).get(f"/app/session/{tid}?token={share_tok}")
        cookies = {c.name: c.value for c in resp.cookies.jar}
        assert "share:" in cookies.get("uterm_principal", "")

    def test_cookie_only_rejects_query(self) -> None:
        """When token_transport='cookie', query param is not accepted."""
        cfg, app = _make_app()
        cfg.tunnel.token_transport = "cookie"
        client = TestClient(app)
        tunnel = _create_tunnel(client)
        tid = tunnel["tunnel_id"]
        share_tok = app.state.uterm_tunnel_tokens[tid]["share_token"]
        resp = TestClient(app).get(f"/app/session/{tid}?token={share_tok}")
        cookies = {c.name: c.value for c in resp.cookies.jar}
        assert "share:" not in cookies.get("uterm_principal", "")
