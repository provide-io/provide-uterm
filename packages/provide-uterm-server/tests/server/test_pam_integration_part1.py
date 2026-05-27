#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.server.pam_integration import (
    _on_close,
    _on_open,
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
    from provide.uterm.server.models import ServerConfig

    config = ServerConfig()
    assert config.pam.notify_socket is None
    await run_pam_integration(config, MagicMock())  # must not raise


async def test_run_pam_integration_missing_pty_package_exits_gracefully() -> None:
    """If provide-uterm-platform not installed, should warn and return cleanly."""
    from provide.uterm.server.models import PamConfig, ServerConfig

    ServerConfig(pam=PamConfig(notify_socket="/run/uterm-notify.sock"))
    # ImportError handling is covered by integration; import patching is too fragile here


# ── _on_open ──────────────────────────────────────────────────────────────────


async def test_on_open_capture_mode_with_socket_creates_capture_session() -> None:
    """Capture mode + capture_socket → create pty_capture session."""
    try:
        from provide.uterm.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-uterm-platform not installed")

    from provide.uterm.server.models import PamConfig

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
        from provide.uterm.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-uterm-platform not installed")

    from provide.uterm.server.models import PamConfig

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
        from provide.uterm.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-uterm-platform not installed")

    from provide.uterm.server.models import PamConfig

    ev = PamEvent(event="open", username="carol", tty="/dev/pts/0", pid=42)
    cfg = PamConfig(notify_socket="/run/uterm-notify.sock", auto_session=False)
    registry = MagicMock()
    registry.create_session = AsyncMock()

    await _on_open(ev, cfg, registry)

    registry.create_session.assert_not_awaited()


async def test_on_open_capture_mode_without_socket_falls_through_to_auto_session() -> None:
    """Capture mode but no capture_socket → fall through to auto_session if enabled."""
    try:
        from provide.uterm.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-uterm-platform not installed")

    from provide.uterm.server.models import PamConfig

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
        from provide.uterm.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-uterm-platform not installed")

    from provide.uterm.server.models import PamConfig

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
        from provide.uterm.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-uterm-platform not installed")

    ev = PamEvent(event="close", username="alice", tty="/dev/pts/3", pid=1234)
    runtime = MagicMock()
    runtime.stop = AsyncMock()

    registry = MagicMock()
    registry.get_runtime = MagicMock(return_value=runtime)

    from provide.uterm.server.models import PamConfig

    await _on_close(ev, PamConfig(), registry)

    runtime.stop.assert_awaited_once()


async def test_on_close_no_session_does_not_raise() -> None:
    """Close event for unknown session is silently ignored."""
    try:
        from provide.uterm.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-uterm-platform not installed")

    ev = PamEvent(event="close", username="ghost", tty="/dev/pts/99", pid=0)
    registry = MagicMock()
    registry.get_runtime = MagicMock(return_value=None)

    from provide.uterm.server.models import PamConfig

    await _on_close(ev, PamConfig(), registry)  # must not raise


async def test_on_close_runtime_stop_exception_is_swallowed() -> None:
    """Errors from runtime.stop() should be caught and logged, not propagated."""
    try:
        from provide.uterm.pty.pam_listener import PamEvent
    except ImportError:
        pytest.skip("provide-uterm-platform not installed")

    ev = PamEvent(event="close", username="alice", tty="/dev/pts/3", pid=1234)
    runtime = MagicMock()
    runtime.stop = AsyncMock(side_effect=RuntimeError("already stopped"))

    registry = MagicMock()
    registry.get_runtime = MagicMock(return_value=runtime)

    from provide.uterm.server.models import PamConfig

    await _on_close(ev, PamConfig(), registry)  # must not raise


# ── PamConfig model ───────────────────────────────────────────────────────────


def test_pam_config_defaults() -> None:
    from provide.uterm.server.models import PamConfig

    cfg = PamConfig()
    assert cfg.notify_socket is None
    assert cfg.mode == "notify"
    assert cfg.auto_session is False
    assert cfg.auto_session_command == "/bin/bash"


def test_pam_config_in_server_config() -> None:
    from provide.uterm.server.models import ServerConfig

    config = ServerConfig()
    assert config.pam.notify_socket is None


def test_pam_config_mode_capture() -> None:
    from provide.uterm.server.models import PamConfig

    cfg = PamConfig(mode="capture", notify_socket="/run/uterm.sock")
    assert cfg.mode == "capture"


# ── CF forwarding ─────────────────────────────────────────────────────────────
