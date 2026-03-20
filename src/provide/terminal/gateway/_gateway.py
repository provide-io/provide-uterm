#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Gateway classes: TelnetWsGateway and SshWsGateway."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import collections.abc

from provide.telemetry import get_logger

from provide.terminal.defaults import TerminalDefaults
from provide.terminal.gateway._colors import _apply_color_mode

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# IAC telnet constants
# ---------------------------------------------------------------------------

_IAC = 255
_SE = 240
_SB = 250
_WILL = 251
_WONT = 252
_DO = 253
_DONT = 254
_BREAK = 243
_IP = 244
_AO = 245
_EOF = 236

# ---------------------------------------------------------------------------
# Token file helpers
# ---------------------------------------------------------------------------


def _read_token(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except FileNotFoundError:
        return None


def _write_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token)


def _delete_token(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


# ---------------------------------------------------------------------------
# JSON control message handler
# ---------------------------------------------------------------------------


async def _handle_ws_control(
    message: str,
    token_file: Path | None,
    write_fn: collections.abc.Callable[[bytes], collections.abc.Coroutine[object, object, None]],
) -> bool:
    """Return True if message was a JSON control frame (intercept it)."""
    try:
        data = json.loads(message)
        msg_type = data.get("type") if isinstance(data, dict) else None
    except (json.JSONDecodeError, ValueError):
        return False
    if msg_type == "session_token" and token_file and "token" in data:
        _write_token(token_file, data["token"])
        return True
    if msg_type == "resume_ok":
        await write_fn(b"\r\n[Session resumed]\r\n")
        return True
    if msg_type == "resume_failed":
        if token_file:
            _delete_token(token_file)
        return True
    return False


# ---------------------------------------------------------------------------
# CRLF normalization
# ---------------------------------------------------------------------------


def _normalize_crlf(raw: bytes) -> bytes:
    """Normalize bare \\n → \\r\\n for telnet clients."""
    return raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


# ---------------------------------------------------------------------------
# IAC telnet negotiation stripper
# ---------------------------------------------------------------------------


def _strip_iac(data: bytes) -> bytes:
    """Remove IAC telnet negotiation sequences from inbound client data."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b != _IAC:
            out.append(b)
            i += 1
            continue
        if i + 1 >= n:
            break
        cmd = data[i + 1]
        if cmd == _IAC:
            out.append(_IAC)
            i += 2
            continue
        if cmd == _SB:
            i += 2
            while i < n:
                if data[i] == _IAC and i + 1 < n and data[i + 1] == _SE:
                    i += 2
                    break
                i += 1
            continue
        if cmd in (_IP, _BREAK):
            out.append(0x03)  # Ctrl-C
            i += 2
            continue
        if cmd == _EOF:
            out.append(0x04)  # Ctrl-D
            i += 2
            continue
        if cmd == _AO:
            i += 2
            continue
        if cmd in (_WILL, _WONT, _DO, _DONT):
            if i + 2 >= n:
                break
            i += 3
            continue
        i += 2
    return bytes(out)


# ---------------------------------------------------------------------------
# Websockets requirement check
# ---------------------------------------------------------------------------


def _require_websockets() -> None:
    try:
        import websockets  # noqa: F401
    except ImportError as exc:
        raise ImportError("websockets is required for gateway support: pip install 'provide-terminal[cli]'") from exc


# ---------------------------------------------------------------------------
# Shared pump helpers
# ---------------------------------------------------------------------------


async def _tcp_to_ws(reader: asyncio.StreamReader, ws: object, *, telnet: bool = False) -> None:
    """Forward raw TCP bytes → WebSocket text frames."""
    while True:
        data = await reader.read(4096)
        if not data:
            break
        if telnet:
            data = _strip_iac(data)
            if not data:
                continue
        await ws.send(data.decode("latin-1", errors="replace"))  # type: ignore[attr-defined]


async def _ws_to_tcp(
    ws: object,
    writer: asyncio.StreamWriter,
    *,
    token_file: Path | None = None,
    color_mode: str = "passthrough",
) -> None:
    """Forward WebSocket messages → raw TCP bytes."""

    async def _write_fn(data: bytes) -> None:
        writer.write(data)
        await writer.drain()

    async for message in ws:  # type: ignore[attr-defined]
        if isinstance(message, str):
            if await _handle_ws_control(message, token_file, _write_fn):
                continue
            raw = message.encode("latin-1", errors="replace")
        else:
            raw = message
        raw = raw.replace(b"\x7f", b"\x08")  # DEL→BS
        raw = _normalize_crlf(raw)
        raw = _apply_color_mode(raw, color_mode)
        writer.write(raw)
        await writer.drain()


async def _pipe_ws(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    ws_url: str,
    *,
    token_file: Path | None = None,
    color_mode: str = "passthrough",
    telnet: bool = False,
) -> None:
    """Open a WebSocket to *ws_url* and bidirectionally pipe with reader/writer."""
    import websockets

    async with websockets.connect(ws_url) as ws:
        token = _read_token(token_file) if token_file else None
        if token:
            await ws.send(json.dumps({"type": "resume", "token": token}))
        t1 = asyncio.create_task(_tcp_to_ws(reader, ws, telnet=telnet))
        t2 = asyncio.create_task(_ws_to_tcp(ws, writer, token_file=token_file, color_mode=color_mode))
        _done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*[*_done, *pending], return_exceptions=True)


# ---------------------------------------------------------------------------
# SSH pump helpers
# ---------------------------------------------------------------------------


async def _ssh_to_ws(process: object, ws: object) -> None:
    """Forward SSH stdin → WebSocket text frames."""
    stdin = process.stdin  # type: ignore[attr-defined]
    while True:
        try:
            data = await stdin.read(4096)
        except Exception:
            break
        if not data:
            break
        await ws.send(data if isinstance(data, str) else data.decode("latin-1", errors="replace"))  # type: ignore[attr-defined]


async def _ws_to_ssh(
    ws: object,
    process: object,
    *,
    token_file: Path | None = None,
    color_mode: str = "passthrough",
) -> None:
    """Forward WebSocket messages → SSH stdout."""
    stdout = process.stdout  # type: ignore[attr-defined]

    async def _write_fn(data: bytes) -> None:
        stdout.write(data.decode("utf-8", errors="replace"))

    async for message in ws:  # type: ignore[attr-defined]
        if isinstance(message, str):
            if await _handle_ws_control(message, token_file, _write_fn):
                continue
            raw = message.encode("latin-1", errors="replace")
            raw = _apply_color_mode(raw, color_mode)
            stdout.write(raw.decode("latin-1", errors="replace"))
        else:
            raw = _apply_color_mode(message, color_mode)
            stdout.write(raw.decode("latin-1", errors="replace"))


# ---------------------------------------------------------------------------
# SSH server helpers (module-level for testability)
# ---------------------------------------------------------------------------


def _make_no_auth_server_class() -> type:
    """Return an asyncssh.SSHServer subclass that accepts all connections."""
    import asyncssh

    class _NoAuthServer(asyncssh.SSHServer):
        # begin_auth returns False → no credentials required from any SSH
        # client.  This is intentional: the gateway trusts the caller to
        # provide network-level access control.  Do NOT bind host="0.0.0.0"
        # on a public interface without an external firewall or auth layer.
        def begin_auth(self, username: str) -> bool:  # noqa: ARG002
            return False

    return _NoAuthServer


async def _make_process_handler(
    ws_url: str,
    token_file: Path | None,
    color_mode: str,
) -> collections.abc.Callable[[object], collections.abc.Coroutine[object, object, None]]:
    """Return an asyncssh process_factory coroutine bound to ws_url/token_file/color_mode."""

    async def _process_handler(process: object) -> None:
        try:
            import websockets

            async with websockets.connect(ws_url) as ws:
                token = _read_token(token_file) if token_file else None
                if token:
                    await ws.send(json.dumps({"type": "resume", "token": token}))
                t1 = asyncio.create_task(_ssh_to_ws(process, ws))
                t2 = asyncio.create_task(_ws_to_ssh(ws, process, token_file=token_file, color_mode=color_mode))
                _done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*[*_done, *pending], return_exceptions=True)
        except Exception as exc:
            logger.debug("ssh_ws_session_ended: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                process.exit(0)  # type: ignore[attr-defined]

    return _process_handler


# ---------------------------------------------------------------------------
# TelnetWsGateway
# ---------------------------------------------------------------------------


class TelnetWsGateway:
    """Raw TCP (telnet) listener that proxies connections to a WebSocket server.

    Each inbound TCP connection gets its own outbound WebSocket connection.
    Both directions are pumped concurrently; whichever side closes first
    cancels the other and the TCP connection is cleaned up.

    Args:
        ws_url: WebSocket URL of the upstream terminal server
            (e.g. ``"wss://warp.provide.io/ws/terminal"``).
        token_file: Path to persist the resume token.  When set, the gateway
            sends a ``{"type": "resume", "token": "..."}`` message on
            reconnect if a token is on disk, and saves new tokens received
            from the server.
        color_mode: ANSI color downgrade mode — ``"passthrough"`` (default),
            ``"256"``, or ``"16"``.

    Example::

        gw = TelnetWsGateway("wss://warp.provide.io/ws/terminal")
        server = await gw.start(port=2112)
        await server.serve_forever()
    """

    def __init__(
        self,
        ws_url: str,
        *,
        token_file: Path | None = None,
        color_mode: str = "passthrough",
    ) -> None:
        _require_websockets()
        self._ws_url = ws_url
        self._token_file = token_file
        self._color_mode = color_mode

    async def start(
        self,
        host: str = TerminalDefaults.BIND_ALL,  # nosec B104
        port: int = TerminalDefaults.GATEWAY_TELNET_PORT,
    ) -> asyncio.AbstractServer:
        """Start the TCP listener and return the server object.

        Args:
            host: Bind address. Defaults to ``"0.0.0.0"``.
            port: TCP port. Defaults to ``2112``.

        Returns:
            An :class:`asyncio.AbstractServer` — call
            ``await server.serve_forever()`` to block until shutdown.
        """
        return await asyncio.start_server(self._handle, host, port)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await _pipe_ws(
                reader,
                writer,
                self._ws_url,
                token_file=self._token_file,
                color_mode=self._color_mode,
                telnet=True,
            )
        except Exception as exc:
            logger.debug("telnet_ws_session_ended: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()


# ---------------------------------------------------------------------------
# SshWsGateway
# ---------------------------------------------------------------------------


class SshWsGateway:
    """SSH server that proxies shell sessions to a WebSocket terminal server.

    Accepts standard SSH client connections (``ssh``, ``putty``, etc.).
    Each shell channel gets its own outbound WebSocket connection and the
    I/O is bridged bidirectionally.

    Requires the ``[ssh]`` extra (asyncssh)::

        pip install 'provide-terminal[cli,ssh]'

    Args:
        ws_url: WebSocket URL of the upstream terminal server.
        server_key: Path to a PEM-encoded SSH host private key file.
            If ``None`` an ephemeral RSA key is generated for each run.
        token_file: Path to persist the resume token.
        color_mode: ANSI color downgrade mode — ``"passthrough"`` (default),
            ``"256"``, or ``"16"``.

    Example::

        gw = SshWsGateway("wss://warp.provide.io/ws/terminal")
        server = await gw.start(port=2222)
        await server.wait_closed()
    """

    def __init__(
        self,
        ws_url: str,
        *,
        server_key: str | Path | None = None,
        token_file: Path | None = None,
        color_mode: str = "passthrough",
    ) -> None:
        _require_websockets()
        try:
            import asyncssh  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "asyncssh is required for SSH gateway support: pip install 'provide-terminal[ssh]'"
            ) from exc
        self._ws_url = ws_url
        self._server_key = server_key
        self._token_file = token_file
        self._color_mode = color_mode

    async def start(
        self, host: str = TerminalDefaults.BIND_ALL, port: int = TerminalDefaults.GATEWAY_SSH_PORT
    ) -> object:  # nosec B104
        """Start the SSH server and return the server object.

        Args:
            host: Bind address. Defaults to ``"0.0.0.0"``.
            port: TCP port. Defaults to ``2222``.

        Returns:
            An asyncssh server object — call ``await server.wait_closed()``
            to block until shutdown.
        """
        import asyncssh

        if self._server_key:
            key_path = Path(self._server_key)
            if not key_path.exists():
                raise FileNotFoundError(f"SSH host key not found: {key_path}")
            if not key_path.is_file():
                raise ValueError(f"SSH host key path is not a file: {key_path}")
            host_keys = [asyncssh.read_private_key(str(key_path))]
        else:
            host_keys = [asyncssh.generate_private_key("ssh-ed25519")]

        no_auth_server_cls = _make_no_auth_server_class()
        process_handler = await _make_process_handler(self._ws_url, self._token_file, self._color_mode)

        return await asyncssh.create_server(
            no_auth_server_cls,
            host,
            port,
            server_host_keys=host_keys,
            process_factory=process_handler,
        )
