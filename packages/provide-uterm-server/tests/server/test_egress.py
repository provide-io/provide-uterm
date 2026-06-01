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
from provide.uterm.server.registry import SessionValidationError

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
# M7: IPv4-mapped IPv6 metadata-IP bypass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connector_mapped_ipv6_metadata_literal_blocked() -> None:
    """A literal IPv4-mapped IPv6 metadata address (::ffff:169.254.169.254)
    must be blocked even with block_private=False — it normalizes to the IPv4
    metadata IP before the membership check."""
    from provide.uterm.server.egress import EgressBlockedError, assert_connector_target_allowed

    with pytest.raises(EgressBlockedError, match="metadata"):
        await assert_connector_target_allowed("::ffff:169.254.169.254", block_private=False)


@pytest.mark.asyncio
async def test_connector_mapped_ipv6_metadata_resolved_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DNS name (AAAA rebind) resolving to the mapped metadata form must be blocked."""
    from provide.uterm.server import egress as egress_mod
    from provide.uterm.server.egress import EgressBlockedError, assert_connector_target_allowed

    monkeypatch.setattr(egress_mod, "_resolve_host", AsyncMock(return_value=("::ffff:169.254.169.254",)))
    with pytest.raises(EgressBlockedError, match="metadata"):
        await assert_connector_target_allowed("rebind.example.com", block_private=False)


@pytest.mark.asyncio
async def test_connector_mapped_ipv6_private_blocked_when_flag() -> None:
    """A mapped-IPv6 private address (::ffff:10.0.0.5) is blocked when block_private=True
    because it normalizes to the private IPv4 before the private check."""
    from provide.uterm.server.egress import EgressBlockedError, assert_connector_target_allowed

    with pytest.raises(EgressBlockedError, match="internal"):
        await assert_connector_target_allowed("::ffff:10.0.0.5", block_private=True)


@pytest.mark.asyncio
async def test_connector_normal_ipv6_loopback_still_allowed_by_default() -> None:
    """A genuine (non-mapped) IPv6 loopback ::1 is unaffected and allowed by default."""
    from provide.uterm.server.egress import assert_connector_target_allowed

    # Must not raise — no ipv4_mapped, behaves as before.
    await assert_connector_target_allowed("::1", block_private=False)


@pytest.mark.asyncio
async def test_connector_normal_ipv6_public_allowed() -> None:
    """A public IPv6 address (no ipv4_mapped) is allowed regardless of block_private."""
    from provide.uterm.server.egress import assert_connector_target_allowed

    await assert_connector_target_allowed("2606:2800:220:1:248:1893:25c8:1946", block_private=False)
    await assert_connector_target_allowed("2606:2800:220:1:248:1893:25c8:1946", block_private=True)


# ---------------------------------------------------------------------------
# M4: embedded-IPv4-in-IPv6 forms bypass the metadata/private check.
#
# egress.py previously normalized ONLY ip.ipv4_mapped (::ffff:a.b.c.d).  Other
# IPv6 forms that embed an IPv4 address (NAT64 well-known prefix 64:ff9b::/96,
# 6to4 2002::/16, deprecated IPv4-compatible ::a.b.c.d) passed straight through
# and were NOT decoded — so 64:ff9b::169.254.169.254 etc. bypassed the metadata
# check.  In a NAT64-enabled IPv6-only cluster that is a real reachable SSRF.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decode_embedded_ipv4_mapped() -> None:
    """_decode_embedded_ipv4 returns the IPv4 for an IPv4-mapped IPv6 (::ffff:a.b.c.d)."""
    import ipaddress

    from provide.uterm.server.egress import _decode_embedded_ipv4

    result = _decode_embedded_ipv4(ipaddress.IPv6Address("::ffff:169.254.169.254"))
    assert result == ipaddress.IPv4Address("169.254.169.254")


@pytest.mark.asyncio
async def test_decode_embedded_ipv4_sixtofour() -> None:
    """_decode_embedded_ipv4 returns the IPv4 for a 6to4 IPv6 (2002:a.b.c.d::)."""
    import ipaddress

    from provide.uterm.server.egress import _decode_embedded_ipv4

    result = _decode_embedded_ipv4(ipaddress.IPv6Address("2002:a9fe:a9fe::"))
    assert result == ipaddress.IPv4Address("169.254.169.254")


@pytest.mark.asyncio
async def test_decode_embedded_ipv4_nat64() -> None:
    """_decode_embedded_ipv4 returns the IPv4 for the NAT64 well-known prefix (64:ff9b::a.b.c.d)."""
    import ipaddress

    from provide.uterm.server.egress import _decode_embedded_ipv4

    result = _decode_embedded_ipv4(ipaddress.IPv6Address("64:ff9b::169.254.169.254"))
    assert result == ipaddress.IPv4Address("169.254.169.254")


@pytest.mark.asyncio
async def test_decode_embedded_ipv4_compatible() -> None:
    """_decode_embedded_ipv4 returns the IPv4 for a deprecated IPv4-compatible IPv6 (::a.b.c.d)."""
    import ipaddress

    from provide.uterm.server.egress import _decode_embedded_ipv4

    result = _decode_embedded_ipv4(ipaddress.IPv6Address("::169.254.169.254"))
    assert result == ipaddress.IPv4Address("169.254.169.254")


@pytest.mark.asyncio
async def test_decode_embedded_ipv4_loopback_not_decoded() -> None:
    """::1 (loopback) must NOT be decoded as an embedded IPv4 (low bits are 1)."""
    import ipaddress

    from provide.uterm.server.egress import _decode_embedded_ipv4

    assert _decode_embedded_ipv4(ipaddress.IPv6Address("::1")) is None


@pytest.mark.asyncio
async def test_decode_embedded_ipv4_unspecified_not_decoded() -> None:
    """:: (unspecified) must NOT be decoded as an embedded IPv4 (low bits are 0)."""
    import ipaddress

    from provide.uterm.server.egress import _decode_embedded_ipv4

    assert _decode_embedded_ipv4(ipaddress.IPv6Address("::")) is None


@pytest.mark.asyncio
async def test_decode_embedded_ipv4_none_for_normal_ipv6() -> None:
    """A normal global IPv6 has no embedded IPv4 form → None."""
    import ipaddress

    from provide.uterm.server.egress import _decode_embedded_ipv4

    assert _decode_embedded_ipv4(ipaddress.IPv6Address("2606:4700::1111")) is None


@pytest.mark.asyncio
async def test_connector_nat64_metadata_blocked() -> None:
    """NAT64 well-known-prefix metadata (64:ff9b::169.254.169.254) decodes to the
    IPv4 metadata IP and must be blocked even with block_private=False."""
    from provide.uterm.server.egress import EgressBlockedError, assert_connector_target_allowed

    with pytest.raises(EgressBlockedError, match="metadata"):
        await assert_connector_target_allowed("64:ff9b::169.254.169.254", block_private=False)


@pytest.mark.asyncio
async def test_connector_sixtofour_metadata_blocked() -> None:
    """6to4 metadata (2002:a9fe:a9fe:: == 6to4 of 169.254.169.254) decodes and is blocked."""
    from provide.uterm.server.egress import EgressBlockedError, assert_connector_target_allowed

    with pytest.raises(EgressBlockedError, match="metadata"):
        await assert_connector_target_allowed("2002:a9fe:a9fe::", block_private=False)


@pytest.mark.asyncio
async def test_connector_ipv4_compatible_metadata_blocked() -> None:
    """IPv4-compatible metadata (::169.254.169.254) decodes and is blocked."""
    from provide.uterm.server.egress import EgressBlockedError, assert_connector_target_allowed

    with pytest.raises(EgressBlockedError, match="metadata"):
        await assert_connector_target_allowed("::169.254.169.254", block_private=False)


@pytest.mark.asyncio
async def test_connector_nat64_private_blocked_when_flag() -> None:
    """NAT64 of a private IPv4 (64:ff9b::10.0.0.1 → 10.0.0.1) is blocked when
    block_private=True — the decoded IPv4 flows through the private-range check,
    not just the metadata check."""
    from provide.uterm.server.egress import EgressBlockedError, assert_connector_target_allowed

    with pytest.raises(EgressBlockedError, match="internal"):
        await assert_connector_target_allowed("64:ff9b::10.0.0.1", block_private=True)


@pytest.mark.asyncio
async def test_connector_nat64_public_allowed_by_default() -> None:
    """NAT64 of a public IPv4 (64:ff9b::8.8.8.8 → 8.8.8.8) is allowed when block_private=False."""
    from provide.uterm.server.egress import assert_connector_target_allowed

    await assert_connector_target_allowed("64:ff9b::8.8.8.8", block_private=False)


# ---------------------------------------------------------------------------
# DNS resolution failure must fail CLOSED (V-H1 review): an OSError /
# socket.gaierror from the resolver must become EgressBlockedError, not
# propagate out (→ HTTP 500) or hang.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connector_resolve_oserror_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resolution OSError (e.g. socket.gaierror) must raise EgressBlockedError, not propagate."""
    import socket

    from provide.uterm.server import egress as egress_mod
    from provide.uterm.server.egress import EgressBlockedError, assert_connector_target_allowed

    monkeypatch.setattr(egress_mod, "_resolve_host", AsyncMock(side_effect=socket.gaierror("no such host")))
    with pytest.raises(EgressBlockedError, match="could not resolve"):
        await assert_connector_target_allowed("hostile-resolver.example.invalid", block_private=False)


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
    with patch("provide.uterm.server.routes.tunnels.assert_session_egress_allowed") as mock_guard:
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


# ---------------------------------------------------------------------------
# V-H1: SSRF chokepoint in SessionRegistry.create_session / update_session.
#
# The egress guard previously had ONE call site (/api/connect).  Every other
# route that creates/updates a session (POST /api/sessions, POST
# /api/profiles/{id}/connect, restart/connect) reached the connectors WITHOUT
# any egress validation — so an authenticated low-priv user could POST a
# metadata IP and reach the cloud-metadata service.  The guard now lives inside
# the registry chokepoint, covering every route by construction.
# ---------------------------------------------------------------------------

_METADATA_IP = "169.254.169.254"


@pytest.fixture()
def metadata_block_app() -> tuple[TestClient, str, dict[str, str]]:
    """TestClient (header auth, operator role) + a created profile id for the
    bypass tests.  Returns (client, profile_id, headers)."""
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    config.recording.directory = Path(tempfile.mkdtemp())
    config.profiles.directory = Path(tempfile.mkdtemp())
    app = create_server_app(config)
    client = TestClient(app)
    headers = {"x-uterm-principal": "user1", "x-uterm-role": "operator"}
    # Create a telnet profile pointing at the metadata IP — the profile-connect
    # route builds connector_config from this host.
    r = client.post(
        "/api/profiles",
        json={
            "name": "evil",
            "connector_type": "telnet",
            "host": _METADATA_IP,
            "port": 23,
        },
        headers=headers,
    )
    assert r.status_code in (200, 201), r.text
    profile_id = r.json()["profile_id"]
    return client, profile_id, headers


def test_create_session_telnet_metadata_ip_rejected(
    metadata_block_app: tuple[TestClient, str, dict[str, str]],
) -> None:
    """RED proof of the SSRF bypass: POST /api/sessions with a telnet connector
    targeting the cloud-metadata IP MUST be rejected (422).  Before the registry
    chokepoint existed this route had no egress guard and the request succeeded,
    reaching 169.254.169.254."""
    client, _profile_id, headers = metadata_block_app
    r = client.post(
        "/api/sessions",
        json={
            "session_id": "ssrf-bypass",
            "connector_type": "telnet",
            "connector_config": {"host": _METADATA_IP, "port": 80},
            "auto_start": True,
        },
        headers=headers,
    )
    assert r.status_code == 422, r.text
    assert "metadata" in r.json()["detail"].lower()


def test_create_session_websocket_metadata_url_rejected(
    metadata_block_app: tuple[TestClient, str, dict[str, str]],
) -> None:
    """POST /api/sessions with a websocket connector whose url host is the
    metadata IP must be rejected at the registry chokepoint."""
    client, _profile_id, headers = metadata_block_app
    r = client.post(
        "/api/sessions",
        json={
            "session_id": "ssrf-ws",
            "connector_type": "websocket",
            "connector_config": {"url": f"ws://{_METADATA_IP}/"},
            "auto_start": True,
        },
        headers=headers,
    )
    assert r.status_code == 422, r.text
    assert "metadata" in r.json()["detail"].lower()


def test_create_session_shell_no_host_allowed(
    metadata_block_app: tuple[TestClient, str, dict[str, str]],
) -> None:
    """A shell connector (no user-supplied host) is NOT guarded and succeeds."""
    client, _profile_id, headers = metadata_block_app
    r = client.post(
        "/api/sessions",
        json={"session_id": "benign-shell", "connector_type": "shell"},
        headers=headers,
    )
    assert r.status_code == 200, r.text


def test_profile_connect_metadata_ip_rejected(
    metadata_block_app: tuple[TestClient, str, dict[str, str]],
) -> None:
    """POST /api/profiles/{id}/connect to a profile whose host is the metadata
    IP must be rejected — the same chokepoint covers the profile route."""
    client, profile_id, headers = metadata_block_app
    r = client.post(
        f"/api/profiles/{profile_id}/connect",
        json={},
        headers=headers,
    )
    assert r.status_code == 422, r.text
    assert "metadata" in r.json()["detail"].lower()


def test_update_session_to_metadata_host_rejected(
    metadata_block_app: tuple[TestClient, str, dict[str, str]],
) -> None:
    """Changing connector_config.host to the metadata IP via PATCH must be
    rejected by the update_session chokepoint."""
    client, _profile_id, headers = metadata_block_app
    # Create a benign telnet session first.
    created = client.post(
        "/api/sessions",
        json={
            "session_id": "mutate-me",
            "connector_type": "telnet",
            "connector_config": {"host": "93.184.216.34", "port": 23},
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    # Now try to repoint it at the metadata IP.
    r = client.patch(
        "/api/sessions/mutate-me",
        json={"connector_config": {"host": _METADATA_IP, "port": 23}},
        headers=headers,
    )
    assert r.status_code == 422, r.text
    assert "metadata" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Registry-level unit tests for the chokepoint.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_create_session_blocks_metadata_telnet() -> None:
    """SessionRegistry.create_session raises SessionValidationError for a telnet
    connector targeting a metadata IP, regardless of block_private."""
    from tests.server.test_registry import _make_registry  # local import to reuse harness

    reg = _make_registry()
    with pytest.raises(SessionValidationError, match="metadata"):
        await reg.create_session(
            {
                "session_id": "reg-metadata",
                "connector_type": "telnet",
                "connector_config": {"host": _METADATA_IP, "port": 23},
            }
        )


@pytest.mark.asyncio
async def test_registry_create_session_blocks_mapped_ipv6_metadata() -> None:
    """An IPv4-mapped IPv6 metadata literal is normalized and blocked."""
    from tests.server.test_registry import _make_registry

    reg = _make_registry()
    with pytest.raises(SessionValidationError, match="metadata"):
        await reg.create_session(
            {
                "session_id": "reg-mapped",
                "connector_type": "ssh",
                "connector_config": {"host": "::ffff:169.254.169.254", "port": 22},
            }
        )


@pytest.mark.asyncio
async def test_registry_create_session_blocks_private_when_flag() -> None:
    """With block_private=True a private host is rejected at the chokepoint."""
    from tests.server.test_registry import _make_registry

    reg = _make_registry(block_private=True)
    with pytest.raises(SessionValidationError, match="internal"):
        await reg.create_session(
            {
                "session_id": "reg-private",
                "connector_type": "ssh",
                "connector_config": {"host": "10.0.0.5", "port": 22},
            }
        )


@pytest.mark.asyncio
async def test_registry_create_session_allows_public_host() -> None:
    """A benign public host passes the chokepoint (no exception)."""
    from tests.server.test_registry import _make_registry

    reg = _make_registry()
    status = await reg.create_session(
        {
            "session_id": "reg-public",
            "connector_type": "ssh",
            "connector_config": {"host": "93.184.216.34", "port": 22},
        }
    )
    assert status.session_id == "reg-public"


@pytest.mark.asyncio
async def test_registry_create_session_allows_shell_no_host() -> None:
    """A shell connector (no host) is not guarded and succeeds."""
    from tests.server.test_registry import _make_registry

    reg = _make_registry()
    status = await reg.create_session({"session_id": "reg-shell", "connector_type": "shell"})
    assert status.session_id == "reg-shell"


@pytest.mark.asyncio
async def test_registry_update_session_blocks_metadata_host() -> None:
    """update_session re-validates a host change to a metadata IP."""
    from tests.server.test_registry import _make_registry

    reg = _make_registry()
    await reg.create_session(
        {
            "session_id": "reg-update",
            "connector_type": "telnet",
            "connector_config": {"host": "93.184.216.34", "port": 23},
        }
    )
    with pytest.raises(SessionValidationError, match="metadata"):
        await reg.update_session(
            "reg-update",
            {"connector_config": {"host": _METADATA_IP, "port": 23}},
        )


# ---------------------------------------------------------------------------
# assert_session_egress_allowed helper — host-derivation unit tests.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_helper_ssh_derives_host_and_blocks_metadata() -> None:
    from provide.uterm.server.egress import EgressBlockedError, assert_session_egress_allowed

    with pytest.raises(EgressBlockedError, match="metadata"):
        await assert_session_egress_allowed("ssh", {"host": _METADATA_IP}, block_private=False)


@pytest.mark.asyncio
async def test_helper_telnet_derives_host_and_blocks_metadata() -> None:
    from provide.uterm.server.egress import EgressBlockedError, assert_session_egress_allowed

    with pytest.raises(EgressBlockedError, match="metadata"):
        await assert_session_egress_allowed("telnet", {"host": _METADATA_IP}, block_private=False)


@pytest.mark.asyncio
async def test_helper_websocket_derives_host_from_url() -> None:
    from provide.uterm.server.egress import EgressBlockedError, assert_session_egress_allowed

    with pytest.raises(EgressBlockedError, match="metadata"):
        await assert_session_egress_allowed("websocket", {"url": f"ws://{_METADATA_IP}/"}, block_private=False)


@pytest.mark.asyncio
async def test_helper_websocket_no_url_short_circuits() -> None:
    """websocket connector with no url -> no host derived -> no guard, no raise."""
    from provide.uterm.server.egress import assert_session_egress_allowed

    await assert_session_egress_allowed("websocket", {}, block_private=False)


@pytest.mark.asyncio
async def test_helper_ssh_no_host_short_circuits() -> None:
    """ssh connector with no host key -> short-circuits without guarding."""
    from provide.uterm.server.egress import assert_session_egress_allowed

    await assert_session_egress_allowed("ssh", {}, block_private=False)


@pytest.mark.asyncio
async def test_helper_shell_type_short_circuits() -> None:
    """A connector type with no user-supplied host (shell) is never guarded."""
    from provide.uterm.server.egress import assert_session_egress_allowed

    await assert_session_egress_allowed("shell", {"host": _METADATA_IP}, block_private=False)


@pytest.mark.asyncio
async def test_helper_allows_benign_public_host() -> None:
    """A benign public host passes the helper without raising."""
    from provide.uterm.server.egress import assert_session_egress_allowed

    await assert_session_egress_allowed("ssh", {"host": "93.184.216.34"}, block_private=False)
