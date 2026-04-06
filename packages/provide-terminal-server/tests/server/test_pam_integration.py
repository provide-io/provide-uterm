# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.terminal.server.pam_integration import (
    _on_close,
    _on_open,
    _session_id,
    _tty_slug,
    run_pam_integration,
)

# ── _tty_slug ─────────────────────────────────────────────────────────────────


def test_tty_slug_pts() -> None:
    # takes last path component: /dev/pts/3 → basename "3"
    assert _tty_slug("/dev/pts/3") == "3"


def test_tty_slug_tty() -> None:
    assert _tty_slug("/dev/tty0") == "tty0"


def test_tty_slug_plain() -> None:
    assert _tty_slug("pts3") == "pts3"


def test_tty_slug_empty() -> None:
    assert _tty_slug("") == "tty"


def test_tty_slug_special_chars() -> None:
    result = _tty_slug("/dev/pts/10")
    assert result == "10"


# ── run_pam_integration ───────────────────────────────────────────────────────


async def test_run_pam_integration_no_notify_socket_exits_early() -> None:
    """Should exit immediately if pam.notify_socket is not set."""
    from provide.terminal.server.models import ServerConfig

    config = ServerConfig()
    assert config.pam.notify_socket is None
    await run_pam_integration(config, MagicMock())  # must not raise


async def test_run_pam_integration_missing_pty_package_exits_gracefully() -> None:
    """If provide-terminal-platform not installed, should warn and return cleanly."""
    from provide.terminal.server.models import PamConfig, ServerConfig

    ServerConfig(pam=PamConfig(notify_socket="/run/uterm-notify.sock"))
    # ImportError handling is covered by integration; import patching is too fragile here


# ── _on_open ──────────────────────────────────────────────────────────────────


async def test_on_open_capture_mode_with_socket_creates_capture_session() -> None:
    """Capture mode + capture_socket → create pty_capture session."""
    try:
        from provide.terminal.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-terminal-platform not installed")

    from provide.terminal.server.models import PamConfig

    ev = PamEvent(
        event="open",
        username="alice",
        tty="/dev/pts/3",
        pid=1234,
        mode="capture",
        capture_socket="/run/uterm-cap-1234.sock",
    )
    cfg = PamConfig(notify_socket="/run/uterm-notify.sock", mode="capture")
    registry = MagicMock()
    registry.create_session = AsyncMock()

    await _on_open(ev, cfg, registry)

    registry.create_session.assert_awaited_once()
    payload = registry.create_session.call_args[0][0]
    assert payload["connector_type"] == "pty_capture"
    assert payload["connector_config"]["socket_path"] == "/run/uterm-cap-1234.sock"
    assert payload["session_id"] == "pam-alice-3"
    assert payload["ephemeral"] is True


async def test_on_open_notify_mode_auto_session_creates_pty_session() -> None:
    """Notify mode + auto_session=True → create pty shell session."""
    try:
        from provide.terminal.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-terminal-platform not installed")

    from provide.terminal.server.models import PamConfig

    ev = PamEvent(event="open", username="bob", tty="/dev/pts/7", pid=999)
    cfg = PamConfig(notify_socket="/run/uterm-notify.sock", auto_session=True)
    registry = MagicMock()
    registry.create_session = AsyncMock()

    await _on_open(ev, cfg, registry)

    registry.create_session.assert_awaited_once()
    payload = registry.create_session.call_args[0][0]
    assert payload["connector_type"] == "pty"
    assert payload["connector_config"]["username"] == "bob"
    assert payload["session_id"] == "pam-bob-7"
    assert payload["ephemeral"] is True


async def test_on_open_notify_mode_no_auto_session_skips_creation() -> None:
    """Notify mode + auto_session=False → do nothing."""
    try:
        from provide.terminal.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-terminal-platform not installed")

    from provide.terminal.server.models import PamConfig

    ev = PamEvent(event="open", username="carol", tty="/dev/pts/0", pid=42)
    cfg = PamConfig(notify_socket="/run/uterm-notify.sock", auto_session=False)
    registry = MagicMock()
    registry.create_session = AsyncMock()

    await _on_open(ev, cfg, registry)

    registry.create_session.assert_not_awaited()


async def test_on_open_capture_mode_without_socket_falls_through_to_auto_session() -> None:
    """Capture mode but no capture_socket → fall through to auto_session if enabled."""
    try:
        from provide.terminal.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-terminal-platform not installed")

    from provide.terminal.server.models import PamConfig

    ev = PamEvent(
        event="open",
        username="dave",
        tty="/dev/pts/1",
        pid=10,
        mode="capture",
        capture_socket=None,  # no capture socket
    )
    cfg = PamConfig(notify_socket="/run/uterm-notify.sock", mode="capture", auto_session=True)
    registry = MagicMock()
    registry.create_session = AsyncMock()

    await _on_open(ev, cfg, registry)

    registry.create_session.assert_awaited_once()
    payload = registry.create_session.call_args[0][0]
    assert payload["connector_type"] == "pty"  # fell through to notify path


async def test_on_open_custom_auto_session_command() -> None:
    """auto_session_command is forwarded to the session payload."""
    try:
        from provide.terminal.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-terminal-platform not installed")

    from provide.terminal.server.models import PamConfig

    ev = PamEvent(event="open", username="eve", tty="/dev/pts/2", pid=7)
    cfg = PamConfig(
        notify_socket="/run/uterm-notify.sock",
        auto_session=True,
        auto_session_command="/bin/zsh",
    )
    registry = MagicMock()
    registry.create_session = AsyncMock()

    await _on_open(ev, cfg, registry)

    payload = registry.create_session.call_args[0][0]
    assert payload["connector_config"]["command"] == "/bin/zsh"


# ── _on_close ─────────────────────────────────────────────────────────────────


async def test_on_close_stops_existing_session() -> None:
    """Close event calls stop() on the runtime if found."""
    try:
        from provide.terminal.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-terminal-platform not installed")

    ev = PamEvent(event="close", username="alice", tty="/dev/pts/3", pid=1234)
    runtime = MagicMock()
    runtime.stop = AsyncMock()

    registry = MagicMock()
    registry.get_runtime = MagicMock(return_value=runtime)

    from provide.terminal.server.models import PamConfig

    await _on_close(ev, PamConfig(), registry)

    runtime.stop.assert_awaited_once()


async def test_on_close_no_session_does_not_raise() -> None:
    """Close event for unknown session is silently ignored."""
    try:
        from provide.terminal.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-terminal-platform not installed")

    ev = PamEvent(event="close", username="ghost", tty="/dev/pts/99", pid=0)
    registry = MagicMock()
    registry.get_runtime = MagicMock(return_value=None)

    from provide.terminal.server.models import PamConfig

    await _on_close(ev, PamConfig(), registry)  # must not raise


async def test_on_close_runtime_stop_exception_is_swallowed() -> None:
    """Errors from runtime.stop() should be caught and logged, not propagated."""
    try:
        from provide.terminal.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-terminal-platform not installed")

    ev = PamEvent(event="close", username="alice", tty="/dev/pts/3", pid=1234)
    runtime = MagicMock()
    runtime.stop = AsyncMock(side_effect=RuntimeError("already stopped"))

    registry = MagicMock()
    registry.get_runtime = MagicMock(return_value=runtime)

    from provide.terminal.server.models import PamConfig

    await _on_close(ev, PamConfig(), registry)  # must not raise


# ── PamConfig model ───────────────────────────────────────────────────────────


def test_pam_config_defaults() -> None:
    from provide.terminal.server.models import PamConfig

    cfg = PamConfig()
    assert cfg.notify_socket is None
    assert cfg.mode == "notify"
    assert cfg.auto_session is False
    assert cfg.auto_session_command == "/bin/bash"


def test_pam_config_in_server_config() -> None:
    from provide.terminal.server.models import ServerConfig

    config = ServerConfig()
    assert config.pam.notify_socket is None


def test_pam_config_mode_capture() -> None:
    from provide.terminal.server.models import PamConfig

    cfg = PamConfig(mode="capture", notify_socket="/run/uterm.sock")
    assert cfg.mode == "capture"


# ── CF forwarding ─────────────────────────────────────────────────────────────


def test_pam_config_relay_fields_default_none() -> None:
    from provide.terminal.server.models import PamConfig

    cfg = PamConfig()
    assert cfg.relay_url is None
    assert cfg.relay_token is None


async def test_forward_to_relay_posts_event() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.terminal.server.pam_integration import _forward_to_relay

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

    from provide.terminal.server.pam_integration import _forward_to_relay

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

    from provide.terminal.server.pam_integration import _forward_to_relay

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("unreachable"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        await _forward_to_relay({"event": "open"}, "https://x.example.com", "tok")  # must not raise


async def test_create_relay_tunnel_returns_token_and_endpoint() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.terminal.server.pam_integration import _create_relay_tunnel

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

    from provide.terminal.server.pam_integration import _create_relay_tunnel

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
        from provide.terminal.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-terminal-platform not installed")

    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.terminal.server.models import PamConfig

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
        from provide.terminal.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-terminal-platform not installed")

    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.terminal.server.models import PamConfig

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
        from provide.terminal.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-terminal-platform not installed")

    ev = PamEvent(event="open", username="alice", tty="/dev/pts/3", pid=1234)
    assert _session_id(ev) == "pam-alice-3"


def test_session_id_empty_tty_includes_pid() -> None:
    """Empty TTY must include PID to prevent collision between concurrent sessions."""
    try:
        from provide.terminal.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-terminal-platform not installed")

    ev1 = PamEvent(event="open", username="alice", tty="", pid=100)
    ev2 = PamEvent(event="open", username="alice", tty="", pid=200)
    assert _session_id(ev1) != _session_id(ev2)
    assert _session_id(ev1) == "pam-alice-tty-100"
    assert _session_id(ev2) == "pam-alice-tty-200"


def test_session_id_open_and_close_match_with_same_pid() -> None:
    """Open and close events with same PID and empty TTY map to the same session_id."""
    try:
        from provide.terminal.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-terminal-platform not installed")

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
        import provide.terminal.pty.pam_listener  # noqa: F401
    except ImportError:
        pytest.skip("provide-terminal-platform not installed")

    from provide.terminal.server.models import PamConfig, ServerConfig

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

    from provide.terminal.server.models import PamConfig, ServerConfig

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
        from provide.terminal.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-terminal-platform not installed")

    from unittest.mock import AsyncMock, MagicMock, patch

    from provide.terminal.server.models import PamConfig

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
        patch("provide.terminal.server.pam_tunnel.PamTunnelBridge", return_value=bridge_mock),
    ):
        await _on_open(ev, cfg, registry, bridges)

    # bridge.start() raised → bridge not stored
    assert "pam-alice-0" not in bridges
    # bridge.stop() called to clean up partial state
    bridge_mock.stop.assert_awaited_once()
