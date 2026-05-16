#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import fcntl
import hashlib
import os
import pty
import shutil
import signal
import sys
import tempfile
import termios
import time
from typing import Any

from provide.uterm.pty._build import get_capture_lib_path
from provide.uterm.pty._validate import (
    validate_command,
    validate_env,
    validate_username,
)
from provide.uterm.pty.capture import CaptureSocket
from provide.uterm.pty.pam import PamSession
from provide.uterm.pty.uid_map import ResolvedUser, UidMap

_VALID_CONFIG_KEYS = frozenset(
    {
        "command",
        "args",
        "username",
        "password",
        "run_as",
        "run_as_uid",
        "run_as_gid",
        "env",
        "inject",
        "cols",
        "rows",
        "input_mode",
    }
)

_VALID_MODES = frozenset({"open", "hijack"})


def _register() -> None:
    import sys

    try:
        from provide.uterm.server.connectors.registry import register_connector

        register_connector("pty", PTYConnector)  # type: ignore[arg-type]
    except ImportError:
        return
    # connectors/__init__.py computes KNOWN_CONNECTOR_TYPES at import time; if it
    # ran before _register() completed (circular import), refresh it now.
    connectors = sys.modules.get("provide.uterm.server.connectors")
    if connectors is not None and hasattr(connectors, "KNOWN_CONNECTOR_TYPES"):
        from provide.uterm.server.connectors.registry import registered_types

        connectors.KNOWN_CONNECTOR_TYPES = registered_types()  # type: ignore[attr-defined]


class PTYConnector:
    """
    Local PTY connector.  connector_type="pty"

    Authenticates (optionally) via PAM, resolves uid/gid via UidMap,
    forks a child in a PTY as the resolved user, and supervises it.

    All config parameters are validated before any system call.
    """

    def __init__(self, session_id: str, display_name: str, config: dict[str, Any]) -> None:
        unknown = set(config) - _VALID_CONFIG_KEYS
        if unknown:
            raise ValueError(f"unknown config keys for PTYConnector: {sorted(unknown)}")
        if "command" not in config:
            raise ValueError("PTYConnector requires 'command' in connector_config")

        # Validate all inputs before storing anything
        validate_command(config["command"])
        if config.get("username"):
            validate_username(config["username"])
        if config.get("env"):
            validate_env(config["env"])

        self._session_id = session_id
        self._display_name = display_name
        self._command: str = config["command"]
        self._args: list[str] = list(config.get("args") or [])
        self._username: str | None = config.get("username")
        self._password: str | None = config.get("password")
        self._run_as: str | None = config.get("run_as")
        self._run_as_uid: int | None = config.get("run_as_uid")
        self._run_as_gid: int | None = config.get("run_as_gid")
        self._extra_env: dict[str, str] = dict(config.get("env") or {})
        self._inject: bool = bool(config.get("inject", False))  # pragma: no mutate
        self._cols: int = int(config.get("cols", 80))
        self._rows: int = int(config.get("rows", 24))
        self._input_mode: str = str(config.get("input_mode", "open"))

        self._uid_map = UidMap()
        self._master_fd: int | None = None
        self._child_pid: int | None = None
        self._connected = False
        self._paused = False
        self._buffer = ""
        self._capture_socket: CaptureSocket | None = None
        self._capture_tmpdir: str | None = None
        self._pam: PamSession | None = None

    async def start(self) -> None:
        pam_env: dict[str, str] = {}

        if self._username and self._password:
            if os.geteuid() != 0:  # nosec B101 — deliberate privilege check
                raise PermissionError("user-switching via PAM requires the server to run as root")
            self._pam = PamSession()
            self._pam.authenticate(self._username, self._password)
            self._pam.acct_mgmt()
            self._pam.open_session()
            pam_env = self._pam.get_env()

        resolved: ResolvedUser | None = None  # pragma: no mutate
        if self._username or self._run_as or self._run_as_uid is not None:
            resolved = self._uid_map.resolve(
                self._username or "",
                run_as=self._run_as,
                run_as_uid=self._run_as_uid,
                run_as_gid=self._run_as_gid,
            )

        capture_path: str | None = None  # pragma: no mutate
        if self._inject:
            # mkdtemp creates a secure directory owned by the current user (mode 0700)
            self._capture_tmpdir = tempfile.mkdtemp(prefix="uterm-cap-")  # nosec B108
            capture_path = str(__import__("pathlib").Path(self._capture_tmpdir) / "cap.sock")
            self._capture_socket = CaptureSocket(capture_path)
            await self._capture_socket.start()

        env = dict(os.environ)
        env.update(pam_env)
        if resolved:
            env.setdefault("HOME", resolved.home)
            env.setdefault("SHELL", resolved.shell)  # nosec B604
            env.setdefault("USER", resolved.name)
            env.setdefault("LOGNAME", resolved.name)
        env.update(self._extra_env)
        if capture_path:
            env["UTERM_CAPTURE_SOCKET"] = capture_path
            lib_path = get_capture_lib_path()
            if lib_path:
                if sys.platform == "darwin":
                    env["DYLD_INSERT_LIBRARIES"] = str(lib_path)
                    env["DYLD_FORCE_FLAT_NAMESPACE"] = "1"
                else:
                    env["LD_PRELOAD"] = str(lib_path)

        master_fd, slave_fd = pty.openpty()
        fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        # Disable local echo: input written by the server is not reflected back
        # in the master's read stream (the frontend renders its own echo).
        attrs = termios.tcgetattr(slave_fd)
        attrs[3] &= ~termios.ECHO  # lflags: clear ECHO bit
        termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)

        pid = os.fork()  # nosec B110 — deliberate fork for PTY supervision
        if pid == 0:  # pragma: no cover
            # ── child ──────────────────────────────────────────────────────
            os.close(master_fd)
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:  # pragma: no mutate
                os.close(slave_fd)

            if resolved:
                os.setgid(resolved.gid)  # nosec B104 — deliberate privilege drop
                os.initgroups(resolved.name, resolved.gid)
                os.setuid(resolved.uid)  # nosec B104 — deliberate privilege drop

            argv = [self._command, *self._args]
            os.execve(self._command, argv, env)  # noqa: S606  # nosec B606 — validated absolute path
            os._exit(127)  # pragma: no cover
        else:
            # ── parent ─────────────────────────────────────────────────────
            os.close(slave_fd)
            self._master_fd = master_fd
            self._child_pid = pid
            self._connected = True

    async def stop(self) -> None:
        if self._child_pid is not None:
            try:
                os.kill(self._child_pid, signal.SIGHUP)
            except (ProcessLookupError, PermissionError):
                pass
            # Close master fd first so the slave side receives HUP, then wait
            # with WNOHANG. If the child hasn't exited yet escalate to SIGKILL
            # and do a final blocking wait so no zombie is left behind. A zombie
            # child of the mutmut stats/clean-run phase would be caught by the
            # mutmut fork loop's os.wait() as an unregistered PID → KeyError.
            if self._master_fd is not None:
                try:
                    os.close(self._master_fd)
                except OSError:
                    pass
                self._master_fd = None
            try:
                pid_reaped, _ = os.waitpid(self._child_pid, os.WNOHANG)
            except ChildProcessError:
                pid_reaped = self._child_pid  # already gone  # pragma: no mutate
            if pid_reaped == 0:
                # Child still running — escalate to SIGKILL then block-wait
                try:
                    os.kill(self._child_pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    os.waitpid(self._child_pid, 0)
                except ChildProcessError:
                    pass
            self._child_pid = None

        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

        if self._capture_socket is not None:
            await self._capture_socket.stop()
            self._capture_socket = None

        if self._capture_tmpdir is not None:
            shutil.rmtree(self._capture_tmpdir, ignore_errors=True)  # pragma: no mutate
            self._capture_tmpdir = None

        if self._pam is not None:
            self._pam.close_session()
            self._pam = None

        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and self._master_fd is not None

    async def poll_messages(self) -> list[dict[str, Any]]:
        if not self.is_connected() or self._paused:
            return []
        data = self._read_master()
        if data:
            self._buffer += data.decode("utf-8", errors="replace")  # pragma: no mutate
            if len(self._buffer) > 32768:  # pragma: no mutate
                self._buffer = self._buffer[-32768:]
            return [self._snapshot()]
        return []

    async def handle_input(self, data: str) -> list[dict[str, Any]]:
        if self.is_connected() and self._master_fd is not None and not self._paused:
            os.write(self._master_fd, data.encode("utf-8"))  # pragma: no mutate
        return [self._snapshot()]

    async def handle_control(self, action: str) -> list[dict[str, Any]]:
        if action == "pause":
            self._paused = True
        elif action in ("resume", "step"):
            self._paused = False
        return [self._snapshot()]

    async def get_snapshot(self) -> dict[str, Any]:
        return self._snapshot()

    async def get_analysis(self) -> str:
        return (
            f"PTYConnector command={self._command!r} "
            f"connected={self._connected} paused={self._paused} "
            f"inject={self._inject} cols={self._cols} rows={self._rows} "
            f"buffer_len={len(self._buffer)}"
        )

    async def set_mode(self, mode: str) -> list[dict[str, Any]]:
        if mode not in _VALID_MODES:
            raise ValueError(f"invalid mode {mode!r}: must be one of {sorted(_VALID_MODES)}")
        self._input_mode = mode
        return [self._hello(), self._snapshot()]

    async def clear(self) -> list[dict[str, Any]]:
        self._buffer = ""
        return [self._snapshot()]

    def _read_master(self) -> bytes:
        if self._master_fd is None:
            return b""
        try:
            return os.read(self._master_fd, 4096)  # pragma: no mutate
        except BlockingIOError:
            return b""
        except OSError:
            self._connected = False
            return b""

    def _snapshot(self) -> dict[str, Any]:
        screen = self._buffer
        return {
            "type": "snapshot",
            "screen": screen,
            "cursor": {"row": 0, "col": 0},
            "cols": self._cols,
            "rows": self._rows,
            "screen_hash": hashlib.md5(screen.encode()).hexdigest(),  # noqa: S324  # nosec B324 — non-crypto change-detection hash
            "cursor_at_end": True,
            "has_trailing_space": False,
            "prompt_detected": False,
            "ts": time.time(),
        }

    def _hello(self) -> dict[str, Any]:
        return {"type": "worker_hello", "input_mode": self._input_mode}


_register()
