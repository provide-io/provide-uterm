#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.server.pam_integration import (
    _on_close,
    _on_open,
    _session_id,
    run_pam_integration,
)

# ── _tty_slug ─────────────────────────────────────────────────────────────────


def test_pam_config_relay_fields_default_none() -> None:
    from provide.uterm.server.models import PamConfig

    cfg = PamConfig()
    assert cfg.relay_url is None
    assert cfg.relay_token is None


async def test_forward_to_relay_posts_event() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.server.pam_integration import _forward_to_relay

    mock_response = MagicMock()
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await _forward_to_relay(
            {"event": "open", "username": "alice", "pid": 1},
            "https://cf.example.com",
            "tok-abc",
        )

    mock_client.post.assert_awaited_once()
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "https://cf.example.com/api/pam-events"
    assert call_args[1]["headers"]["Authorization"] == "Bearer tok-abc"
    assert call_args[1]["json"]["username"] == "alice"


async def test_forward_to_relay_trailing_slash_stripped() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.server.pam_integration import _forward_to_relay

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_client):
        await _forward_to_relay({"event": "close"}, "https://cf.example.com/", "tok")

    url = mock_client.post.call_args[0][0]
    assert url == "https://cf.example.com/api/pam-events"


async def test_forward_to_relay_swallows_network_error() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    import httpx

    from provide.uterm.server.pam_integration import _forward_to_relay

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("unreachable"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        await _forward_to_relay({"event": "open"}, "https://x.example.com", "tok")  # must not raise


async def test_create_relay_tunnel_returns_token_and_endpoint() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.server.pam_integration import _create_relay_tunnel

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(
        return_value={"worker_token": "wt-123", "ws_endpoint": "wss://cf.example.com/tunnel/abc"}
    )
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _create_relay_tunnel("https://cf.example.com", "tok", "pam-alice-3", "alice (/dev/pts/3)")

    assert result == ("wt-123", "wss://cf.example.com/tunnel/abc")
    body = mock_client.post.call_args[1]["json"]
    assert body["session_id"] == "pam-alice-3"
    assert body["tunnel_type"] == "terminal"


async def test_create_relay_tunnel_returns_none_on_error() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    import httpx

    from provide.uterm.server.pam_integration import _create_relay_tunnel

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("unreachable"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _create_relay_tunnel("https://cf.example.com", "tok", "s1", "name")

    assert result is None


async def test_on_open_forwards_to_cf_when_configured() -> None:
    """_on_open calls _forward_to_relay when relay_url + relay_token are set."""
    try:
        from provide.uterm.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-uterm-platform not installed")

    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.server.models import PamConfig

    ev = PamEvent(event="open", username="alice", tty="/dev/pts/0", pid=42)
    cfg = PamConfig(
        notify_socket="/run/x.sock",
        relay_url="https://cf.example.com",
        relay_token="tok",
    )
    registry = MagicMock()
    registry.create_session = AsyncMock()

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(
        return_value=MagicMock(
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"worker_token": "t", "ws_endpoint": "wss://x"}),
        )
    )

    with patch("httpx.AsyncClient", return_value=mock_client):
        await _on_open(ev, cfg, registry)

    assert mock_client.post.await_count >= 1


async def test_on_close_forwards_to_cf_when_configured() -> None:
    """_on_close calls _forward_to_relay when relay_url + relay_token are set."""
    try:
        from provide.uterm.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-uterm-platform not installed")

    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.server.models import PamConfig

    ev = PamEvent(event="close", username="alice", tty="/dev/pts/0", pid=42)
    cfg = PamConfig(
        notify_socket="/run/x.sock",
        relay_url="https://cf.example.com",
        relay_token="tok",
    )
    registry = MagicMock()
    registry.get_runtime = MagicMock(return_value=None)

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_client):
        await _on_close(ev, cfg, registry)

    mock_client.post.assert_awaited_once()
    body = mock_client.post.call_args[1]["json"]
    assert body["event"] == "close"
    assert body["username"] == "alice"


# ── _session_id ───────────────────────────────────────────────────────────────


def test_session_id_with_tty_uses_slug_only() -> None:
    try:
        from provide.uterm.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-uterm-platform not installed")

    ev = PamEvent(event="open", username="alice", tty="/dev/pts/3", pid=1234)
    assert _session_id(ev) == "pam-alice-3"


def test_session_id_empty_tty_includes_pid() -> None:
    """Empty TTY must include PID to prevent collision between concurrent sessions."""
    try:
        from provide.uterm.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-uterm-platform not installed")

    ev1 = PamEvent(event="open", username="alice", tty="", pid=100)
    ev2 = PamEvent(event="open", username="alice", tty="", pid=200)
    assert _session_id(ev1) != _session_id(ev2)
    assert _session_id(ev1) == "pam-alice-tty-100"
    assert _session_id(ev2) == "pam-alice-tty-200"


def test_session_id_open_and_close_match_with_same_pid() -> None:
    """Open and close events with same PID and empty TTY map to the same session_id."""
    try:
        from provide.uterm.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-uterm-platform not installed")

    ev_open = PamEvent(event="open", username="bob", tty="", pid=999)
    ev_close = PamEvent(event="close", username="bob", tty="", pid=999)
    assert _session_id(ev_open) == _session_id(ev_close)


# ── run_pam_integration event loop ───────────────────────────────────────────


async def test_run_pam_integration_dispatches_event_via_real_socket() -> None:
    """Full integration: real Unix socket → run_pam_integration → handler called."""
    import asyncio
    import json
    import tempfile
    from pathlib import Path

    try:
        import provide.uterm.pty.pam_listener  # noqa: F401
    except ImportError:
        pytest.skip("provide-uterm-platform not installed")

    from provide.uterm.server.models import PamConfig, ServerConfig

    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "pam-notify.sock")
        config = ServerConfig(pam=PamConfig(notify_socket=sock_path, auto_session=True))

        registry = MagicMock()
        registry.create_session = AsyncMock()

        # Start integration in background task
        task = asyncio.create_task(run_pam_integration(config, registry))
        # Give the listener time to bind
        await asyncio.sleep(0.05)

        # Send a real PAM open event over the socket
        event_line = (
            json.dumps(
                {
                    "event": "open",
                    "username": "testuser",
                    "tty": "/dev/pts/5",
                    "pid": 7777,
                }
            ).encode()
            + b"\n"
        )
        reader, writer = await asyncio.open_unix_connection(sock_path)
        writer.write(event_line)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

        # Wait for handler to be called
        deadline = asyncio.get_event_loop().time() + 2.0
        while asyncio.get_event_loop().time() < deadline:
            if registry.create_session.await_count >= 1:
                break
            await asyncio.sleep(0.05)

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        registry.create_session.assert_awaited_once()
        payload = registry.create_session.call_args[0][0]
        assert payload["session_id"] == "pam-testuser-5"
        assert payload["connector_config"]["username"] == "testuser"


async def test_run_pam_integration_cancelled_cleanly() -> None:
    """Cancelling run_pam_integration does not raise outside CancelledError."""
    import asyncio
    import tempfile
    from pathlib import Path

    from provide.uterm.server.models import PamConfig, ServerConfig

    with tempfile.TemporaryDirectory() as td:
        sock_path = str(Path(td) / "pam-cancel.sock")
        config = ServerConfig(pam=PamConfig(notify_socket=sock_path))
        registry = MagicMock()

        task = asyncio.create_task(run_pam_integration(config, registry))
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # Socket should be cleaned up
        assert not Path(sock_path).exists()


# ── bridge error path ─────────────────────────────────────────────────────────


async def test_on_open_bridge_start_failure_cleans_up() -> None:
    """If PamTunnelBridge.start() raises, bridge.stop() is called for cleanup."""
    try:
        from provide.uterm.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-uterm-platform not installed")

    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.server.models import PamConfig

    ev = PamEvent(event="open", username="alice", tty="/dev/pts/0", pid=42)
    cfg = PamConfig(
        notify_socket="/run/x.sock",
        relay_url="https://cf.example.com",
        relay_token="tok",
    )
    registry = MagicMock()
    registry.create_session = AsyncMock()
    runtime = MagicMock()
    connector = MagicMock()
    runtime.connector = connector
    registry.get_runtime = MagicMock(return_value=runtime)

    bridge_mock = MagicMock()
    bridge_mock.start = AsyncMock(side_effect=RuntimeError("connect failed"))
    bridge_mock.stop = AsyncMock()

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(
        return_value=MagicMock(
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"worker_token": "t", "ws_endpoint": "wss://x"}),
        )
    )

    bridges: dict[str, object] = {}
    with (
        patch("httpx.AsyncClient", return_value=mock_client),
        patch("provide.uterm.server.pam_tunnel.PamTunnelBridge", return_value=bridge_mock),
    ):
        await _on_open(ev, cfg, registry, bridges)

    # bridge.start() raised → bridge not stored
    assert "pam-alice-0" not in bridges
    # bridge.stop() called to clean up partial state
    bridge_mock.stop.assert_awaited_once()


# ── L11: relay egress guard ───────────────────────────────────────────────────


async def test_forward_to_relay_blocked_for_metadata_ip_no_post() -> None:
    """A relay_url whose host is a cloud-metadata IP must be blocked before the
    POST — exfiltrating PAM event data + the relay bearer token to 169.254.169.254
    is refused by the egress guard, and the PAM loop does not crash."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.server.pam_integration import _forward_to_relay

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_client):
        # Literal metadata IP → assert_webhook_target_allowed raises EgressBlockedError.
        await _forward_to_relay({"event": "open"}, "https://169.254.169.254/", "tok")  # must not raise

    # Egress guard fired before the POST → no event/token left the host.
    mock_client.post.assert_not_awaited()


async def test_forward_to_relay_allows_benign_url_posts() -> None:
    """A benign relay_url passes the egress guard and the POST proceeds."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.server.pam_integration import _forward_to_relay

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_client):
        await _forward_to_relay({"event": "open"}, "https://cf.example.com", "tok")

    mock_client.post.assert_awaited_once()


async def test_create_relay_tunnel_blocked_for_metadata_ip_returns_none() -> None:
    """A metadata-IP relay_url blocks the tunnel POST and returns None, mirroring
    the existing failure handling — the loop does not crash."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.server.pam_integration import _create_relay_tunnel

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _create_relay_tunnel("https://169.254.169.254/", "tok", "s1", "name")

    assert result is None
    mock_client.post.assert_not_awaited()


async def test_create_relay_tunnel_allows_benign_url() -> None:
    """A benign relay_url passes the egress guard; the tunnel POST proceeds."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.uterm.server.pam_integration import _create_relay_tunnel

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"worker_token": "wt", "ws_endpoint": "wss://x"})
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _create_relay_tunnel("https://cf.example.com", "tok", "s1", "name")

    assert result == ("wt", "wss://x")
    mock_client.post.assert_awaited_once()
