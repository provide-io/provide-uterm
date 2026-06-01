#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Post-connect peer-IP egress validation (M3 DNS-rebinding residual mitigation).

The create-time egress guard resolves+validates the target host name, but the
connector re-resolves at connect time — a TTL-0 rebind to a metadata IP between
create and connect is otherwise unguarded.  These tests prove that the SSH and
WebSocket connectors validate the *actual* connected peer IP right after the
transport handshake completes and BEFORE any application/PTY data flows, and
abort if the peer is a blocked (cloud-metadata) target.

CRITICAL: this validation must NOT weaken TLS SNI / certificate validation or
SSH host-key/known-hosts verification — the handshake still uses the original
hostname.  The `test_no_verification_weakening` grep test in
tests/server/test_egress_peer_ip.py asserts no verification bypass was added.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.server.egress import EgressBlockedError

_METADATA_IP = "169.254.169.254"
_PUBLIC_IP = "93.184.216.34"


# ---------------------------------------------------------------------------
# assert_ip_allowed — literal-IP (no-DNS) variant of the connector guard.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_ip_allowed_blocks_metadata_always() -> None:
    """A literal metadata IP is blocked even with block_private=False."""
    from provide.uterm.server.egress import assert_ip_allowed

    with pytest.raises(EgressBlockedError, match="metadata"):
        assert_ip_allowed(_METADATA_IP, block_private=False)


@pytest.mark.asyncio
async def test_assert_ip_allowed_allows_public_by_default() -> None:
    """A routable public IP is allowed."""
    from provide.uterm.server.egress import assert_ip_allowed

    # Must not raise.
    assert_ip_allowed(_PUBLIC_IP, block_private=False)


@pytest.mark.asyncio
async def test_assert_ip_allowed_private_allowed_by_default() -> None:
    """A private IP is allowed when block_private=False."""
    from provide.uterm.server.egress import assert_ip_allowed

    assert_ip_allowed("10.0.0.5", block_private=False)


@pytest.mark.asyncio
async def test_assert_ip_allowed_private_blocked_when_flag() -> None:
    """A private IP is blocked when block_private=True."""
    from provide.uterm.server.egress import assert_ip_allowed

    with pytest.raises(EgressBlockedError, match="internal"):
        assert_ip_allowed("10.0.0.5", block_private=True)


@pytest.mark.asyncio
async def test_assert_ip_allowed_decodes_mapped_ipv6_metadata() -> None:
    """An IPv4-mapped IPv6 metadata literal decodes and is blocked."""
    from provide.uterm.server.egress import assert_ip_allowed

    with pytest.raises(EgressBlockedError, match="metadata"):
        assert_ip_allowed("::ffff:169.254.169.254", block_private=False)


@pytest.mark.asyncio
async def test_assert_ip_allowed_normal_ipv6_allowed() -> None:
    """A normal global IPv6 (no embedded IPv4) is allowed by default."""
    from provide.uterm.server.egress import assert_ip_allowed

    assert_ip_allowed("2606:2800:220:1:248:1893:25c8:1946", block_private=False)


# ---------------------------------------------------------------------------
# WebSocket connector — peer-IP validation after the handshake.
# ---------------------------------------------------------------------------


def _make_ws_connector(url: str = "wss://benign.example.com/term") -> Any:
    from provide.uterm.server.connectors.websocket import WebSocketSessionConnector

    return WebSocketSessionConnector("ws-sess", "WS", {"url": url})


@pytest.mark.asyncio
async def test_websocket_aborts_on_metadata_peer() -> None:
    """A rebind to a metadata peer IP aborts BEFORE the connector is marked live."""
    c = _make_ws_connector()
    fake_ws = MagicMock()
    fake_ws.remote_address = (_METADATA_IP, 443)
    fake_ws.close = AsyncMock()
    with (
        patch("websockets.connect", new=AsyncMock(return_value=fake_ws)),
        pytest.raises(EgressBlockedError, match="metadata"),
    ):
        await c.start()
    # The connection that reached the metadata IP must be torn down and never
    # become live (no application data may flow).
    fake_ws.close.assert_awaited()
    assert not c.is_connected()
    assert c._ws is None


@pytest.mark.asyncio
async def test_websocket_proceeds_on_benign_peer() -> None:
    """A benign (public) peer IP lets the connection proceed normally."""
    c = _make_ws_connector()
    fake_ws = MagicMock()
    fake_ws.remote_address = (_PUBLIC_IP, 443)
    fake_ws.close = AsyncMock()
    with patch("websockets.connect", new=AsyncMock(return_value=fake_ws)):
        await c.start()
    assert c.is_connected()
    fake_ws.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_websocket_ipv6_peer_tuple_handled() -> None:
    """An IPv6 peername is a 4-tuple (host, port, flowinfo, scopeid); the host
    element (index 0) is used for validation."""
    c = _make_ws_connector()
    fake_ws = MagicMock()
    fake_ws.remote_address = ("::ffff:169.254.169.254", 443, 0, 0)
    fake_ws.close = AsyncMock()
    with (
        patch("websockets.connect", new=AsyncMock(return_value=fake_ws)),
        pytest.raises(EgressBlockedError, match="metadata"),
    ):
        await c.start()
    fake_ws.close.assert_awaited()


@pytest.mark.asyncio
async def test_websocket_missing_peername_proceeds() -> None:
    """If the transport exposes no peername (None), the connection proceeds —
    the create-time guard already validated the resolved name; we only ABORT on
    a positively-identified blocked peer, never fail-closed on a missing peer."""
    c = _make_ws_connector()
    fake_ws = MagicMock()
    fake_ws.remote_address = None
    fake_ws.close = AsyncMock()
    with patch("websockets.connect", new=AsyncMock(return_value=fake_ws)):
        await c.start()
    assert c.is_connected()
    fake_ws.close.assert_not_awaited()


# ---------------------------------------------------------------------------
# Telnet connector — peer-IP validation after the TCP handshake.
# ---------------------------------------------------------------------------


def _make_telnet_connector(config: dict[str, Any] | None = None) -> Any:
    from provide.uterm.server.connectors.telnet import TelnetSessionConnector

    return TelnetSessionConnector("tn-sess", "TN", config or {"host": "benign.example.com", "port": 23})


def _stub_telnet_transport(c: Any, peer_ip: str | None) -> MagicMock:
    """Replace the connector's transport with a MagicMock reporting *peer_ip*."""
    transport = MagicMock()
    transport.connect = AsyncMock()
    transport.disconnect = AsyncMock()
    transport.peer_ip = MagicMock(return_value=peer_ip)
    transport.is_connected = MagicMock(return_value=True)
    c._transport = transport
    return transport


@pytest.mark.asyncio
async def test_telnet_aborts_on_metadata_peer() -> None:
    """A rebind to a metadata peer IP aborts BEFORE the connector is marked live."""
    c = _make_telnet_connector()
    transport = _stub_telnet_transport(c, _METADATA_IP)
    with pytest.raises(EgressBlockedError, match="metadata"):
        await c.start()
    transport.disconnect.assert_awaited()
    assert c._connected is False


@pytest.mark.asyncio
async def test_telnet_proceeds_on_benign_peer() -> None:
    """A benign (public) peer IP lets the connection proceed normally."""
    c = _make_telnet_connector()
    transport = _stub_telnet_transport(c, _PUBLIC_IP)
    await c.start()
    assert c._connected is True
    transport.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_telnet_missing_peer_ip_proceeds() -> None:
    """If the transport exposes no peer IP (None), the connection proceeds —
    the create-time guard already validated the resolved name."""
    c = _make_telnet_connector()
    transport = _stub_telnet_transport(c, None)
    await c.start()
    assert c._connected is True
    transport.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_telnet_aborts_on_mapped_ipv6_metadata_peer() -> None:
    """An IPv4-mapped IPv6 metadata peer literal decodes and is blocked."""
    c = _make_telnet_connector()
    transport = _stub_telnet_transport(c, "::ffff:169.254.169.254")
    with pytest.raises(EgressBlockedError, match="metadata"):
        await c.start()
    transport.disconnect.assert_awaited()
    assert c._connected is False


# ---------------------------------------------------------------------------
# SSH connector — peer-IP validation after the SSH handshake, before the PTY.
# ---------------------------------------------------------------------------


def _make_ssh_connector(config: dict[str, Any] | None = None) -> Any:
    pytest.importorskip("asyncssh", reason="asyncssh not installed")
    from provide.uterm.server.connectors.ssh import SshSessionConnector

    return SshSessionConnector(
        "ssh-sess", "SSH", config or {"host": "benign.example.com", "insecure_no_host_check": True}
    )


@pytest.mark.asyncio
async def test_ssh_aborts_on_metadata_peer_before_process() -> None:
    """A rebind to a metadata peer IP aborts the SSH connection BEFORE
    create_process() (i.e. before any PTY/app data) and closes the connection."""
    import asyncssh

    c = _make_ssh_connector()
    mock_conn = MagicMock()
    mock_conn.get_extra_info = MagicMock(return_value=(_METADATA_IP, 22))
    mock_conn.create_process = AsyncMock()
    mock_conn.close = MagicMock()
    mock_conn.wait_closed = AsyncMock()
    with (
        patch.object(asyncssh, "connect", new=AsyncMock(return_value=mock_conn)),
        pytest.raises(EgressBlockedError, match="metadata"),
    ):
        await c.start()
    # The PTY process must NEVER be created for a blocked peer.
    mock_conn.create_process.assert_not_called()
    mock_conn.close.assert_called_once()
    assert not c.is_connected()
    assert c._conn is None


@pytest.mark.asyncio
async def test_ssh_proceeds_on_benign_peer() -> None:
    """A benign peer IP lets the SSH connection proceed to create_process()."""
    import asyncssh

    c = _make_ssh_connector()
    mock_process = MagicMock()
    mock_process.stdin = MagicMock()
    mock_process.stdout = MagicMock()
    mock_conn = MagicMock()
    mock_conn.get_extra_info = MagicMock(return_value=(_PUBLIC_IP, 22))
    mock_conn.create_process = AsyncMock(return_value=mock_process)
    mock_conn.close = MagicMock()
    mock_conn.wait_closed = AsyncMock()
    with patch.object(asyncssh, "connect", new=AsyncMock(return_value=mock_conn)):
        await c.start()
    assert c.is_connected()
    mock_conn.create_process.assert_awaited_once()
    mock_conn.close.assert_not_called()


@pytest.mark.asyncio
async def test_ssh_missing_peername_proceeds() -> None:
    """If asyncssh exposes no peername, the SSH connection proceeds (the
    create-time guard already validated the resolved name)."""
    import asyncssh

    c = _make_ssh_connector()
    mock_process = MagicMock()
    mock_process.stdin = MagicMock()
    mock_process.stdout = MagicMock()
    mock_conn = MagicMock()
    mock_conn.get_extra_info = MagicMock(return_value=None)
    mock_conn.create_process = AsyncMock(return_value=mock_process)
    mock_conn.close = MagicMock()
    mock_conn.wait_closed = AsyncMock()
    with patch.object(asyncssh, "connect", new=AsyncMock(return_value=mock_conn)):
        await c.start()
    assert c.is_connected()
    mock_conn.create_process.assert_awaited_once()
