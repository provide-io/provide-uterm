#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the egress SSRF guard (egress.py) and the /api/connect route guard."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config

# ---------------------------------------------------------------------------
# Unit tests for assert_connector_target_allowed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_ip_always_blocked() -> None:
    """169.254.169.254 must be blocked even when block_private=False."""
    from provide.uterm.server.egress import EgressBlockedError, assert_connector_target_allowed

    with pytest.raises(EgressBlockedError, match="metadata"):
        await assert_connector_target_allowed("169.254.169.254", block_private=False)


@pytest.mark.asyncio
async def test_metadata_ip_alicloud_always_blocked() -> None:
    """Alibaba Cloud metadata IP 100.100.100.200 is always blocked."""
    from provide.uterm.server.egress import EgressBlockedError, assert_connector_target_allowed

    with pytest.raises(EgressBlockedError, match="metadata"):
        await assert_connector_target_allowed("100.100.100.200", block_private=False)


@pytest.mark.asyncio
async def test_metadata_ipv6_always_blocked() -> None:
    """IPv6 metadata IP fd00:ec2::254 is always blocked."""
    from provide.uterm.server.egress import EgressBlockedError, assert_connector_target_allowed

    with pytest.raises(EgressBlockedError, match="metadata"):
        await assert_connector_target_allowed("fd00:ec2::254", block_private=False)


@pytest.mark.asyncio
async def test_private_ip_allowed_by_default() -> None:
    """10.0.0.5 is allowed when block_private=False (default posture)."""
    from provide.uterm.server.egress import assert_connector_target_allowed

    # Must not raise
    await assert_connector_target_allowed("10.0.0.5", block_private=False)


@pytest.mark.asyncio
async def test_private_ip_blocked_when_flag_set() -> None:
    """10.0.0.5 must be blocked when block_private=True."""
    from provide.uterm.server.egress import EgressBlockedError, assert_connector_target_allowed

    with pytest.raises(EgressBlockedError, match="internal"):
        await assert_connector_target_allowed("10.0.0.5", block_private=True)


@pytest.mark.asyncio
async def test_loopback_blocked_when_flag_set() -> None:
    """127.0.0.1 must be blocked when block_private=True."""
    from provide.uterm.server.egress import EgressBlockedError, assert_connector_target_allowed

    with pytest.raises(EgressBlockedError, match="internal"):
        await assert_connector_target_allowed("127.0.0.1", block_private=True)


@pytest.mark.asyncio
async def test_loopback_allowed_by_default() -> None:
    """127.0.0.1 is allowed when block_private=False (SSH to localhost is legitimate)."""
    from provide.uterm.server.egress import assert_connector_target_allowed

    # Must not raise
    await assert_connector_target_allowed("127.0.0.1", block_private=False)


@pytest.mark.asyncio
async def test_external_ip_always_allowed() -> None:
    """A routable public IP is always allowed regardless of block_private."""
    from provide.uterm.server.egress import assert_connector_target_allowed

    await assert_connector_target_allowed("93.184.216.34", block_private=False)
    await assert_connector_target_allowed("93.184.216.34", block_private=True)


@pytest.mark.asyncio
async def test_dns_resolving_to_metadata_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DNS name that resolves to a metadata IP must be blocked."""
    from provide.uterm.server import egress as egress_mod
    from provide.uterm.server.egress import EgressBlockedError, assert_connector_target_allowed

    monkeypatch.setattr(egress_mod, "_resolve_host", AsyncMock(return_value=("169.254.169.254",)))
    with pytest.raises(EgressBlockedError, match="metadata"):
        await assert_connector_target_allowed("evil.example.com", block_private=False)


@pytest.mark.asyncio
async def test_unresolvable_name_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresolvable hostname must raise EgressBlockedError."""
    from provide.uterm.server import egress as egress_mod
    from provide.uterm.server.egress import EgressBlockedError, assert_connector_target_allowed

    monkeypatch.setattr(egress_mod, "_resolve_host", AsyncMock(return_value=()))
    with pytest.raises(EgressBlockedError, match="could not resolve"):
        await assert_connector_target_allowed("nxdomain.example.invalid", block_private=False)


@pytest.mark.asyncio
async def test_dns_resolving_to_private_blocked_when_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DNS name resolving to a private IP must be blocked when block_private=True."""
    from provide.uterm.server import egress as egress_mod
    from provide.uterm.server.egress import EgressBlockedError, assert_connector_target_allowed

    monkeypatch.setattr(egress_mod, "_resolve_host", AsyncMock(return_value=("192.168.1.100",)))
    with pytest.raises(EgressBlockedError, match="internal"):
        await assert_connector_target_allowed("internal.corp.example.com", block_private=True)


@pytest.mark.asyncio
async def test_dns_resolving_to_private_allowed_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DNS name resolving to a private IP is allowed when block_private=False."""
    from provide.uterm.server import egress as egress_mod
    from provide.uterm.server.egress import assert_connector_target_allowed

    monkeypatch.setattr(egress_mod, "_resolve_host", AsyncMock(return_value=("192.168.1.100",)))
    # Must not raise
    await assert_connector_target_allowed("internal.corp.example.com", block_private=False)


@pytest.mark.asyncio
async def test_bracketed_ipv6_literal_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host supplied as [::1] (URL brackets) is handled as a literal IP."""
    from provide.uterm.server.egress import assert_connector_target_allowed

    # Loopback IPv6 is allowed when block_private=False
    await assert_connector_target_allowed("[::1]", block_private=False)


@pytest.mark.asyncio
async def test_bracketed_ipv6_literal_blocked_when_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host supplied as [::1] (URL brackets) is blocked when block_private=True."""
    from provide.uterm.server.egress import EgressBlockedError, assert_connector_target_allowed

    with pytest.raises(EgressBlockedError, match="internal"):
        await assert_connector_target_allowed("[::1]", block_private=True)


# ---------------------------------------------------------------------------
# Route-level integration tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def connect_client() -> TestClient:
    """TestClient with header auth, operator role so quick_connect is allowed."""
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    config.recording.directory = Path(tempfile.mkdtemp())
    app = create_server_app(config)
    return TestClient(app)


def test_quick_connect_websocket_metadata_ip_returns_422(connect_client: TestClient) -> None:
    """POST /api/connect with a metadata IP as the websocket URL must return 422."""
    r = connect_client.post(
        "/api/connect",
        json={
            "connector_type": "websocket",
            "url": "ws://169.254.169.254/",
        },
        headers={"x-uterm-principal": "user1", "x-uterm-role": "operator"},
    )
    assert r.status_code == 422
    assert "metadata" in r.json()["detail"].lower()


def test_quick_connect_ssh_metadata_ip_returns_422(connect_client: TestClient) -> None:
    """POST /api/connect with a metadata IP as the ssh host must return 422."""
    r = connect_client.post(
        "/api/connect",
        json={
            "connector_type": "ssh",
            "host": "169.254.169.254",
            "port": 22,
        },
        headers={"x-uterm-principal": "user1", "x-uterm-role": "operator"},
    )
    assert r.status_code == 422
    assert "metadata" in r.json()["detail"].lower()


def test_quick_connect_telnet_metadata_ip_returns_422(connect_client: TestClient) -> None:
    """POST /api/connect with a metadata IP as the telnet host must return 422."""
    r = connect_client.post(
        "/api/connect",
        json={
            "connector_type": "telnet",
            "host": "169.254.169.254",
            "port": 23,
        },
        headers={"x-uterm-principal": "user1", "x-uterm-role": "operator"},
    )
    assert r.status_code == 422
    assert "metadata" in r.json()["detail"].lower()


def test_quick_connect_internal_host_allowed_by_default(connect_client: TestClient) -> None:
    """POST /api/connect to an internal SSH host passes the guard when block_private=False (default)."""
    with patch("provide.uterm.server.routes.tunnels.assert_connector_target_allowed") as mock_guard:
        mock_guard.return_value = None  # allowed
        # Also mock create_session to avoid a real connector attempt
        with patch("provide.uterm.server.registry.SessionRegistry.create_session") as mock_cs:
            from provide.uterm.server.config_schema import SessionDefinition

            mock_cs.return_value = SessionDefinition(
                session_id="connect-abc123",
                connector_type="ssh",
                display_name="ssh",
            )
            r = connect_client.post(
                "/api/connect",
                json={"connector_type": "ssh", "host": "10.0.0.5", "port": 22},
                headers={"x-uterm-principal": "user1", "x-uterm-role": "operator"},
            )
        # Guard was called (not skipped)
        mock_guard.assert_called_once()
    # Should proceed past the guard — 200 or 422 from session validation is fine
    assert r.status_code in (200, 422)


def test_quick_connect_shell_skips_egress_guard(connect_client: TestClient) -> None:
    """Shell connector (no host/url) must skip the egress guard entirely."""
    with patch("provide.uterm.server.egress.assert_connector_target_allowed") as mock_guard:
        r = connect_client.post(
            "/api/connect",
            json={"connector_type": "shell"},
            headers={"x-uterm-principal": "user1", "x-uterm-role": "operator"},
        )
        mock_guard.assert_not_called()
    assert r.status_code == 200


def test_internal_tunnel_path_skips_egress_guard(connect_client: TestClient) -> None:
    """POST /api/tunnels (no user-supplied host/url) must not invoke the egress guard."""
    with patch("provide.uterm.server.egress.assert_connector_target_allowed") as mock_guard:
        r = connect_client.post(
            "/api/tunnels",
            json={"tunnel_type": "terminal"},
            headers={"x-uterm-principal": "user1", "x-uterm-role": "operator"},
        )
        mock_guard.assert_not_called()
    # 200 or 422/409 depending on registry — guard must NOT be called
    assert r.status_code in (200, 409, 422)


def test_quick_connect_websocket_no_url_skips_guard(connect_client: TestClient) -> None:
    """websocket connector with no url in payload must skip the egress guard."""
    with patch("provide.uterm.server.egress.assert_connector_target_allowed") as mock_guard:
        r = connect_client.post(
            "/api/connect",
            json={"connector_type": "websocket"},
            headers={"x-uterm-principal": "user1", "x-uterm-role": "operator"},
        )
        mock_guard.assert_not_called()
    # May succeed or fail validation (no url), but guard must not fire
    assert r.status_code in (200, 422)


def test_config_block_private_connector_targets_default_false() -> None:
    """SecurityConfig.block_private_connector_targets defaults to False."""
    from provide.uterm.server.config_schema import SecurityConfig

    cfg = SecurityConfig()
    assert cfg.block_private_connector_targets is False


def test_config_block_private_connector_targets_can_be_enabled() -> None:
    """SecurityConfig.block_private_connector_targets can be set to True."""
    from provide.uterm.server.config_schema import SecurityConfig

    cfg = SecurityConfig(block_private_connector_targets=True)
    assert cfg.block_private_connector_targets is True
