#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""SSH-backed connector for the hosted server app."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from typing import Any

from provide.telemetry import get_logger

from provide.terminal.defaults import TerminalDefaults
from provide.terminal.screen import decode_cp437
from provide.terminal.server.connectors.base import SessionConnector

try:
    import asyncssh
except ImportError as _e:  # pragma: no cover
    raise ImportError("asyncssh is required for the SSH server connector: pip install 'provide-terminal[ssh]'") from _e

logger = get_logger(__name__)
_COLS = 80
_ROWS = 25


class SshSessionConnector(SessionConnector):
    """Connect a hosted session to a remote SSH shell."""

    _VALID_CONFIG_KEYS: frozenset[str] = frozenset(
        {
            "host",
            "port",
            "username",
            "password",
            "client_keys",
            "client_key_path",
            "client_key",
            "client_key_data",
            "known_hosts",
            "insecure_no_host_check",
            "input_mode",
        }
    )

    @staticmethod
    def _load_client_keys(config: dict[str, Any]) -> list[Any]:
        """Build the list of SSH client keys from the connector config."""
        raw_client_keys = config.get("client_keys")
        client_keys: list[Any] = []
        if raw_client_keys is not None:
            if isinstance(raw_client_keys, (list, tuple)):
                client_keys.extend(item for item in raw_client_keys if item is not None)
            else:
                client_keys.append(raw_client_keys)
        if config.get("client_key_path") is not None:
            client_keys.append(str(config["client_key_path"]))
        if config.get("client_key") is not None:
            client_keys.append(str(config["client_key"]))
        if config.get("client_key_data") is not None:
            key_data = config["client_key_data"]
            if isinstance(key_data, bytes):
                imported_key = asyncssh.import_private_key(key_data)
            else:
                imported_key = asyncssh.import_private_key(str(key_data).encode("utf-8"))
            client_keys.append(imported_key)
        return client_keys

    def __init__(self, session_id: str, display_name: str, config: dict[str, Any]) -> None:
        unknown = set(config) - self._VALID_CONFIG_KEYS
        if unknown:
            raise ValueError(f"unknown ssh connector_config keys: {sorted(unknown)}")
        client_keys = SshSessionConnector._load_client_keys(config)
        self._session_id = session_id
        self._display_name = display_name
        self._host = str(config.get("host", TerminalDefaults.TELNET_HOST))
        self._port = int(config.get("port", TerminalDefaults.SSH_REMOTE_PORT))
        self._username = str(config.get("username", "guest"))
        self._password = None if config.get("password") is None else str(config.get("password"))
        self._client_keys = client_keys
        self._known_hosts = None if config.get("known_hosts") is None else str(config.get("known_hosts"))
        if self._known_hosts is None:
            if not config.get("insecure_no_host_check"):
                raise ValueError(
                    f"ssh_connector requires known_hosts for session {session_id!r} connecting to {self._host}; "
                    "set connector_config.known_hosts to a known_hosts file path, "
                    "or set insecure_no_host_check=true to disable host key verification"
                )
            logger.warning(
                "ssh_connector_no_known_hosts session_id=%s host=%s — "
                "host key verification is disabled; set known_hosts in connector_config to enable it",
                session_id,
                self._host,
            )
        self._input_mode = str(config.get("input_mode", "open"))
        self._paused = False
        self._connected = False
        self._bytes_received = 0
        self._banner = f"Connected to ssh://{self._username}@{self._host}:{self._port}"
        self._screen_buffer = ""
        self._conn: asyncssh.SSHClientConnection | None = None
        self._process: asyncssh.SSHClientProcess[Any] | None = None
        self._stdin: Any = None
        self._stdout: Any = None

    def _screen(self) -> str:
        header = [
            f"\x1b[1;35m[{self._display_name} ({self._session_id})]\x1b[0m",
            "-" * 60,
            f"\x1b[32mUpstream:\x1b[0m ssh://{self._username}@{self._host}:{self._port}",
            f"\x1b[32mMode:\x1b[0m {'Shared input' if self._input_mode == 'open' else 'Exclusive hijack'}",
            f"\x1b[32mControl:\x1b[0m {'Paused for hijack' if self._paused else 'Live'}",
            f"\x1b[33m{self._banner}\x1b[0m",
            "",
        ]
        return "\n".join((header + self._screen_buffer.splitlines())[-_ROWS:])

    def _snapshot(self) -> dict[str, Any]:
        screen = self._screen()
        lines = screen.splitlines() or [""]
        last = lines[-1]
        return {
            "type": "snapshot",
            "screen": screen,
            "cursor": {"x": min(len(last), _COLS - 1), "y": min(len(lines) - 1, _ROWS - 1)},
            "cols": _COLS,
            "rows": _ROWS,
            "screen_hash": hashlib.sha256(screen.encode("utf-8")).hexdigest()[:16],
            "cursor_at_end": True,
            "has_trailing_space": False,
            "prompt_detected": {"prompt_id": "ssh_stream"},
            "ts": time.time(),
        }

    def _hello(self) -> dict[str, Any]:
        return {"type": "worker_hello", "input_mode": self._input_mode, "ts": time.time()}

    async def start(self) -> None:
        conn = await asyncssh.connect(
            self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            known_hosts=self._known_hosts,
            config=[],
            client_keys=self._client_keys,
            encoding=None,
            connect_timeout=30,
        )
        process = await conn.create_process(term_type="ansi", term_size=(_COLS, _ROWS), encoding=None)
        self._conn = conn
        self._process = process
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._connected = True

    async def stop(self) -> None:
        conn = self._conn
        self._conn = None
        process = self._process
        self._process = None
        stdin = self._stdin
        self._stdin = None
        self._stdout = None
        self._connected = False
        if stdin is not None:
            with contextlib.suppress(Exception):
                stdin.write_eof()
        if process is not None:
            with contextlib.suppress(Exception):
                process.close()
        if conn is not None:
            conn.close()
            with contextlib.suppress(Exception):
                await conn.wait_closed()

    def is_connected(self) -> bool:
        return self._connected and self._stdout is not None and self._stdin is not None and self._conn is not None

    async def poll_messages(self) -> list[dict[str, Any]]:
        stdout = self._stdout
        if not self.is_connected() or stdout is None:
            return []
        try:
            data = await asyncio.wait_for(stdout.read(4096), timeout=0.1)
        except TimeoutError:
            return []
        if not data:
            return []
        payload = data.encode("latin-1", errors="replace") if isinstance(data, str) else data
        self._bytes_received += len(payload)
        text = decode_cp437(payload)
        self._screen_buffer = (self._screen_buffer + text)[-32_000:]
        self._banner = f"Received {self._bytes_received} bytes from SSH upstream."
        return [{"type": "term", "data": text, "ts": time.time()}, self._snapshot()]

    async def handle_input(self, data: str) -> list[dict[str, Any]]:
        stdin = self._stdin
        if stdin is not None:
            stdin.write(data.encode("utf-8", errors="replace"))
            await stdin.drain()
            self._banner = f"Sent {len(data)} characters upstream."
        return [self._snapshot()]

    async def handle_control(self, action: str) -> list[dict[str, Any]]:
        if action == "pause":
            self._paused = True
            self._banner = "Exclusive control active."
        elif action == "resume":
            self._paused = False
            self._banner = "Exclusive control released."
        elif action == "step":
            self._banner = "Step requested. Awaiting upstream output."
        else:
            self._banner = f"Ignored control action: {action}"
        return [self._snapshot()]

    async def get_snapshot(self) -> dict[str, Any]:
        return self._snapshot()

    async def get_analysis(self) -> str:
        return "\n".join(
            [
                f"[ssh session analysis — worker: {self._session_id}]",
                f"host: {self._host}",
                f"port: {self._port}",
                f"user: {self._username}",
                f"input_mode: {self._input_mode}",
                f"paused: {self._paused}",
                f"bytes_received: {self._bytes_received}",
                f"connected: {self.is_connected()}",
            ]
        )

    async def clear(self) -> list[dict[str, Any]]:
        self._screen_buffer = ""
        self._banner = "Screen buffer cleared."
        return [self._snapshot()]

    async def set_mode(self, mode: str) -> list[dict[str, Any]]:
        if mode not in {"open", "hijack"}:
            raise ValueError(f"invalid mode: {mode}")
        self._input_mode = mode
        if mode == "open":
            self._paused = False
        self._banner = f"Input mode set to {'Shared input' if mode == 'open' else 'Exclusive hijack'}."
        return [self._hello(), self._snapshot()]


from provide.terminal.server.connectors.registry import register_connector  # noqa: E402

register_connector("ssh", SshSessionConnector)
