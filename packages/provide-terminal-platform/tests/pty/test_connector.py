# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PTYConnector: init/validation, lifecycle, register, and is_connected tests."""

from __future__ import annotations

import asyncio
import os
import sys
from types import ModuleType
from unittest.mock import patch

import pytest

from provide.terminal.pty.connector import PTYConnector

from ._connector_helpers import make_connector

# ── Validation ────────────────────────────────────────────────────────────────

def test_connector_requires_command() -> None:
    with pytest.raises(ValueError, match="command"):
        PTYConnector("s1", "name", config={})


def test_connector_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unknown config"):
        PTYConnector("s1", "name", config={"command": "/bin/echo", "unknown_key": True})


def test_connector_rejects_relative_command() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        make_connector("bash")


def test_connector_rejects_null_byte_in_command() -> None:
    with pytest.raises(ValueError, match="null byte"):
        make_connector("/bin/bash\x00")


def test_connector_rejects_null_byte_in_username() -> None:
    with pytest.raises(ValueError, match="null byte"):
        make_connector(username="ali\x00ce")


def test_connector_rejects_null_byte_in_env_value() -> None:
    with pytest.raises(ValueError, match="null byte"):
        make_connector(env={"KEY": "val\x00ue"})


def test_connector_rejects_env_key_with_equals() -> None:
    with pytest.raises(ValueError, match="invalid key"):
        make_connector(env={"KEY=BAD": "value"})


def test_connector_requires_command_exact_message() -> None:
    """Error message starts with exact text — kills XX-prefix mutations.

    Uses anchored pattern so 'XXPTYConnector...' mutations are caught.
    """
    with pytest.raises(ValueError, match=r"^PTYConnector requires 'command'"):
        PTYConnector("s1", "n", config={})


# ── Init state ────────────────────────────────────────────────────────────────

def test_connector_init_default_state() -> None:
    """__init__ stores all config values and sets correct initial state.

    Verifies defaults for cols/rows/inject and that internal state fields
    are set to their exact initial values.
    """
    conn = PTYConnector(
        "sid-1",
        "Display Name",
        config={
            "command": "/bin/bash",
            "args": ["--norc"],
            "env": {"FOO": "bar"},
            "cols": 120,
            "rows": 40,
            "inject": True,
        },
    )
    assert conn._session_id == "sid-1"
    assert conn._display_name == "Display Name"
    assert conn._command == "/bin/bash"
    assert conn._args == ["--norc"]
    assert conn._extra_env == {"FOO": "bar"}
    assert conn._cols == 120
    assert conn._rows == 40
    assert conn._inject is True
    assert conn._master_fd is None
    assert conn._child_pid is None
    assert conn._connected is False
    assert conn._paused is False
    assert conn._input_mode == "open"
    assert conn._buffer == ""
    assert conn._capture_socket is None
    assert conn._capture_tmpdir is None
    assert conn._pam is None
    # Default cols/rows/inject
    conn2 = make_connector("/bin/echo")
    assert conn2._cols == 80
    assert conn2._rows == 24
    assert conn2._inject is False


def test_connector_init_run_as_fields() -> None:
    """__init__ stores run_as, run_as_uid, run_as_gid from config correctly."""
    conn = PTYConnector(
        "s1",
        "n",
        config={"command": "/bin/bash", "run_as": "operator", "run_as_uid": 1001, "run_as_gid": 1002},
    )
    assert conn._run_as == "operator"
    assert conn._run_as_uid == 1001
    assert conn._run_as_gid == 1002


# ── is_connected ──────────────────────────────────────────────────────────────

def test_is_connected_before_start() -> None:
    assert make_connector().is_connected() is False


def test_is_connected_requires_both_flag_and_fd() -> None:
    """is_connected() requires both _connected=True AND _master_fd set.

    Kills the 'and' → 'or' mutation.
    """
    conn = make_connector()
    conn._connected = True
    assert conn.is_connected() is False


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def test_start_and_stop_echo() -> None:
    conn = make_connector("/bin/echo", ["hello from pty"])
    await conn.start()
    assert conn.is_connected() is True
    screens: list[str] = []
    for _ in range(20):
        await asyncio.sleep(0.1)
        msgs = await conn.poll_messages()
        screens.extend(m["screen"] for m in msgs if m.get("type") == "snapshot")
        if any("hello from pty" in s for s in screens):
            break
    await conn.stop()
    assert conn.is_connected() is False
    assert conn._connected is False
    assert any("hello from pty" in s for s in screens)


async def test_stop_without_start_is_safe() -> None:
    await make_connector().stop()


async def test_start_with_run_as_uid_sets_env() -> None:
    conn = make_connector("/bin/echo", ["x"], run_as_uid=os.geteuid())
    await conn.start()
    assert conn.is_connected()
    await conn.stop()


async def test_start_master_fd_is_nonblocking() -> None:
    """start() sets master_fd to O_NONBLOCK.

    Kills mutations that remove or change the F_SETFL call (fcntl mutmut_104/105),
    which would leave master_fd in blocking mode. This test checks the flag directly
    (fast path) so it fails immediately instead of hanging on os.read().
    """
    import fcntl as _fcntl

    conn = make_connector("/bin/cat")
    await conn.start()
    assert conn._master_fd is not None
    fl = _fcntl.fcntl(conn._master_fd, _fcntl.F_GETFL)
    await conn.stop()
    assert fl & os.O_NONBLOCK, "master_fd must be O_NONBLOCK after start()"


async def test_start_pam_requires_root() -> None:
    if os.geteuid() == 0:
        pytest.skip("test only applies when not root")
    conn = make_connector("/bin/echo", username="nobody", password="pass")
    with pytest.raises(PermissionError, match="root"):
        await conn.start()
    await conn.stop()


async def test_start_pam_requires_root_mocked() -> None:
    """start() raises PermissionError with exact PAM message when not root.

    Uses anchored pattern so 'XXuser-switching...' mutations are caught.
    """
    with patch("provide.terminal.pty.connector.os.geteuid", return_value=1000):
        conn = make_connector("/bin/echo", username="nobody", password="pass")
        with pytest.raises(PermissionError, match=r"^user-switching via PAM"):
            await conn.start()
    await conn.stop()


async def test_start_pam_path_mocked_as_root() -> None:
    """start() with mocked root calls all PAM methods."""
    from unittest.mock import MagicMock

    mock_pam = MagicMock()
    mock_pam.get_env.return_value = {}
    with (
        patch("provide.terminal.pty.connector.os.geteuid", return_value=0),
        patch("provide.terminal.pty.connector.PamSession", return_value=mock_pam),
    ):
        conn = make_connector("/bin/echo", ["x"], username="nobody", password="secret")
        await conn.start()
        await conn.stop()
    mock_pam.authenticate.assert_called_once_with("nobody", "secret")
    mock_pam.acct_mgmt.assert_called_once()
    mock_pam.open_session.assert_called_once()
    mock_pam.get_env.assert_called_once()


async def test_start_username_without_password_skips_pam() -> None:
    """username-only (no password) skips PAM — kills 'and' → 'or' mutation."""
    if os.geteuid() == 0:
        pytest.skip("test only applies when not root")
    conn = make_connector("/bin/echo", ["x"], username="nobody")
    await conn.start()
    await conn.stop()


# ── _register ─────────────────────────────────────────────────────────────────

def test_register_import_error_silently_returns() -> None:
    from provide.terminal.pty.connector import _register

    with patch.dict(sys.modules, {"provide.terminal.server.connectors.registry": None}):
        _register()


def test_register_no_refresh_when_connectors_module_absent() -> None:
    from provide.terminal.pty.connector import PTYConnector, _register

    fake_registry = ModuleType("provide.terminal.server.connectors.registry")
    calls: list[tuple[str, object]] = []

    fake_registry.register_connector = lambda n, c: calls.append((n, c))  # type: ignore[attr-defined]
    fake_registry.registered_types = lambda: frozenset({"pty"})  # type: ignore[attr-defined]

    with patch.dict(sys.modules, {
        "provide.terminal.server.connectors.registry": fake_registry,
        "provide.terminal.server.connectors": None,
    }):
        _register()
    assert calls == [("pty", PTYConnector)]


def test_register_refreshes_known_connector_types() -> None:
    from provide.terminal.pty.connector import PTYConnector, _register

    fake_registry = ModuleType("provide.terminal.server.connectors.registry")
    calls: list[tuple[str, object]] = []

    fake_registry.register_connector = lambda n, c: calls.append((n, c))  # type: ignore[attr-defined]
    fake_registry.registered_types = lambda: frozenset({"pty"})  # type: ignore[attr-defined]

    fake_connectors = ModuleType("provide.terminal.server.connectors")
    fake_connectors.KNOWN_CONNECTOR_TYPES = frozenset()  # type: ignore[attr-defined]

    with patch.dict(sys.modules, {
        "provide.terminal.server.connectors.registry": fake_registry,
        "provide.terminal.server.connectors": fake_connectors,
    }):
        _register()
    assert calls == [("pty", PTYConnector)]
    assert "pty" in fake_connectors.KNOWN_CONNECTOR_TYPES


def test_register_no_refresh_when_known_connector_types_absent() -> None:
    """Kills 'and' → 'or' mutation in the hasattr guard."""
    from provide.terminal.pty.connector import _register

    fake_registry = ModuleType("provide.terminal.server.connectors.registry")
    refresh_calls: list[int] = []

    fake_registry.register_connector = lambda n, c: None  # type: ignore[attr-defined]
    fake_registry.registered_types = lambda: refresh_calls.append(1) or frozenset({"pty"})  # type: ignore[attr-defined]

    fake_connectors = ModuleType("provide.terminal.server.connectors")

    with patch.dict(sys.modules, {
        "provide.terminal.server.connectors.registry": fake_registry,
        "provide.terminal.server.connectors": fake_connectors,
    }):
        _register()
    assert refresh_calls == []


# ── Root-only ─────────────────────────────────────────────────────────────────

@pytest.mark.requires_root
async def test_user_switch_requires_root() -> None:
    conn = make_connector("/usr/bin/id", username="nobody")
    await conn.start()
    await asyncio.sleep(0.2)
    msgs = await conn.poll_messages()
    await conn.stop()
    screens = [m["screen"] for m in msgs if m.get("type") == "snapshot"]
    assert any("nobody" in s for s in screens)
