#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Gateway classes: TelnetWsGateway and SshWsGateway."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import collections.abc

from provide.telemetry import get_logger
from provide.terminal.control_channel import (
    ControlChannelDecoder,
    ControlChannelProtocolError,
    ControlChunk,
    DataChunk,
    encode_control,
    encode_data,
)
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
# JSON control message handler
# ---------------------------------------------------------------------------


async def _handle_ws_control(
    message: str,
    token_holder: list[dict | None],
    write_fn: collections.abc.Callable[[bytes], collections.abc.Coroutine[object, object, None]],
) -> bool:
    """Return True if *message* is a gateway control frame (intercept it)."""
    try:
        decoder = ControlChannelDecoder()
        events = decoder.feed(message)
        events.extend(decoder.finish())
    except ControlChannelProtocolError:
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        return await _handle_ws_control_frame(data, token_holder, write_fn)

    if not events:
        return False
    handled = False
    for event in events:
        if isinstance(event, DataChunk):
            return False
        handled = await _handle_ws_control_frame(event.control, token_holder, write_fn) or handled
    return handled


async def _handle_ws_control_frame(
    data: dict[str, object],
    token_holder: list[dict | None],
    write_fn: collections.abc.Callable[[bytes], collections.abc.Coroutine[object, object, None]],
) -> bool:
    try:
        msg_type = data.get("type") if isinstance(data.get("type"), str) else None
    except AttributeError:
        return False
    if msg_type == "session_token" and "token" in data:
        pid = data.get("player_id")
        token_dict: dict[str, object] = {"token": str(data["token"])}
        if isinstance(pid, int):
            token_dict["player_id"] = pid
        token_holder[0] = token_dict
        return True
    if msg_type == "resume_ok":
        await write_fn(b"\r\n[Session resumed]\r\n")
        return True
    if msg_type == "resume_failed":
        token_holder[0] = None
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


def _skip_subneg_sequence(data: bytes, i: int, n: int) -> int:
    """Scan forward from *i* to find the end of an IAC SB … IAC SE sequence.

    *i* should point to the first byte **after** the IAC SB opener (i.e. the
    start of the subnegotiation payload).

    Returns the position immediately after the closing IAC SE pair, or *n* if
    the sequence is truncated.
    """
    while i < n:
        if data[i] == _IAC and i + 1 < n and data[i + 1] == _SE:
            return i + 2
        i += 1
    return n


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
            i = _skip_subneg_sequence(data, i + 2, n)
            continue
        if cmd in (_IP, _BREAK):
            out.append(0x03)  # Ctrl-C
            i += 2
            continue
        if cmd == _EOF:
            out.append(0x04)  # Ctrl-D
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
        await ws.send(encode_data(data.decode("latin-1", errors="replace")))  # type: ignore[attr-defined]


async def _ws_to_tcp(
    ws: object,
    writer: asyncio.StreamWriter,
    *,
    token_holder: list[dict | None],
    color_mode: str = "passthrough",
) -> None:
    """Forward WebSocket messages → raw TCP bytes."""
    decoder = ControlChannelDecoder()

    async def _write_fn(data: bytes) -> None:
        writer.write(data)
        await writer.drain()

    async for message in ws:  # type: ignore[attr-defined]
        if isinstance(message, str):
            try:
                events = decoder.feed(message)
            except ControlChannelProtocolError:
                continue
            for event in events:
                if isinstance(event, ControlChunk):
                    await _handle_ws_control_frame(event.control, token_holder, _write_fn)
                    continue
                raw = event.data.encode("latin-1", errors="replace")
                raw = raw.replace(b"\x7f", b"\x08")  # DEL→BS
                raw = _normalize_crlf(raw)
                raw = _apply_color_mode(raw, color_mode)
                writer.write(raw)
                await writer.drain()
            continue
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
    token_holder: list[dict | None],
    color_mode: str = "passthrough",
    telnet: bool = False,
) -> None:
    """Open a WebSocket to *ws_url* and bidirectionally pipe with reader/writer."""
    import websockets

    async with websockets.connect(ws_url) as ws:
        token_data = token_holder[0]
        if token_data:
            resume_msg: dict[str, object] = {"type": "resume", "token": token_data["token"]}
            if "player_id" in token_data:
                resume_msg["player_id"] = token_data["player_id"]
            await ws.send(encode_control(resume_msg))
        t1 = asyncio.create_task(_tcp_to_ws(reader, ws, telnet=telnet))
        t2 = asyncio.create_task(_ws_to_tcp(ws, writer, token_holder=token_holder, color_mode=color_mode))
        _done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:  # pragma: no branch — may be empty if both finish
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
        payload = data if isinstance(data, str) else data.decode("latin-1", errors="replace")
        await ws.send(encode_data(payload))  # type: ignore[attr-defined]


async def _ws_to_ssh(
    ws: object,
    process: object,
    *,
    token_holder: list[dict | None],
    color_mode: str = "passthrough",
) -> None:
    """Forward WebSocket messages → SSH stdout."""
    stdout = process.stdout  # type: ignore[attr-defined]
    decoder = ControlChannelDecoder()

    async def _write_fn(data: bytes) -> None:
        stdout.write(data.decode("utf-8", errors="replace"))

    async for message in ws:  # type: ignore[attr-defined]
        if isinstance(message, str):
            try:
                events = decoder.feed(message)
            except ControlChannelProtocolError:
                continue
            for event in events:
                if isinstance(event, ControlChunk):
                    await _handle_ws_control_frame(event.control, token_holder, _write_fn)
                    continue
                raw = event.data.encode("latin-1", errors="replace")
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
    color_mode: str,
) -> collections.abc.Callable[[object], collections.abc.Coroutine[object, object, None]]:
    """Return an asyncssh process_factory coroutine bound to ws_url/color_mode."""

    async def _process_handler(process: object) -> None:
        max_reconnects = 12
        reconnect_delay = 3.0
        stdin = process.stdin  # type: ignore[attr-defined]
        stdout = process.stdout  # type: ignore[attr-defined]

        # Per-connection in-memory token. Starts empty; updated when the server
        # sends a session_token frame. Discarded when this coroutine returns.
        token_holder: list[dict | None] = [None]

        try:
            import websockets

            for attempt in range(max_reconnects + 1):
                # SSH client disconnected — nothing to do
                if hasattr(stdin, "at_eof") and stdin.at_eof():
                    break

                try:
                    async with websockets.connect(ws_url) as ws:
                        token_data = token_holder[0]
                        if token_data:
                            resume_msg: dict[str, object] = {"type": "resume", "token": token_data["token"]}
                            if "player_id" in token_data:
                                resume_msg["player_id"] = token_data["player_id"]
                            await ws.send(encode_control(resume_msg))
                        t1 = asyncio.create_task(_ssh_to_ws(process, ws))
                        t2 = asyncio.create_task(_ws_to_ssh(ws, process, token_holder=token_holder, color_mode=color_mode))
                        _done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
                        for task in pending:  # pragma: no branch — may be empty if both finish
                            task.cancel()
                        await asyncio.gather(*[*_done, *pending], return_exceptions=True)
                except Exception as exc:
                    logger.debug("ssh_ws_pipe_error attempt=%d: %s", attempt, exc)

                # SSH client disconnected — done
                if hasattr(stdin, "at_eof") and stdin.at_eof():
                    break

                # WS closed but SSH client still connected — show reconnect indicator
                if attempt < max_reconnects:
                    logger.debug(
                        "ssh_ws_disconnected: reconnecting in %.1fs (attempt %d/%d)",
                        reconnect_delay,
                        attempt + 1,
                        max_reconnects,
                    )
                    try:
                        stdout.write("\x1b7\x1b[999;1H\x1b[2;36m* reconnecting...\x1b[0m\x1b8")
                    except Exception:
                        pass
                    await asyncio.sleep(reconnect_delay)

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

    If the upstream WebSocket closes while the TCP client is still connected
    (e.g. Cloudflare DO hibernation), the gateway reconnects automatically
    using the session token received in-memory from the server. The token is
    never written to disk and is discarded when the TCP client disconnects.

    Args:
        ws_url: WebSocket URL of the upstream terminal server
            (e.g. ``"wss://warp.provide.io/ws/terminal"``).
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
        color_mode: str = "passthrough",
    ) -> None:
        _require_websockets()
        self._ws_url = ws_url
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
        """Handle one inbound telnet connection, reconnecting on WS-side drops.

        When the upstream WebSocket closes unexpectedly (e.g. Cloudflare DO
        hibernation) while the TCP client is still connected, this method
        waits briefly and reconnects — using the in-memory resume token so
        the DO restores the session seamlessly.  If the TCP client closes
        first, no retry is attempted.
        """
        max_reconnects = 12
        reconnect_delay = 3.0

        # Per-connection in-memory token. Starts empty; updated when the server
        # sends a session_token frame. Discarded when this method returns.
        token_holder: list[dict | None] = [None]

        try:
            for attempt in range(max_reconnects + 1):
                if reader.at_eof():
                    break
                try:
                    await _pipe_ws(
                        reader,
                        writer,
                        self._ws_url,
                        token_holder=token_holder,
                        color_mode=self._color_mode,
                        telnet=True,
                    )
                except Exception as exc:
                    logger.debug("telnet_ws_pipe_error attempt=%d: %s", attempt, exc)

                # TCP client closed — we're done
                if reader.at_eof():
                    break

                # WS closed while TCP is still alive (hibernation or transient drop)
                if attempt < max_reconnects:
                    logger.debug(
                        "ws_disconnected_tcp_alive: reconnecting in %.1fs (attempt %d/%d)",
                        reconnect_delay,
                        attempt + 1,
                        max_reconnects,
                    )
                    # Show a reconnect indicator on the bottom row so telnet/SSH
                    # clients get the same feedback as the browser WebSocket client.
                    # Uses save/restore cursor so the game display is not disturbed.
                    try:
                        writer.write(b"\x1b7\x1b[999;1H\x1b[2;36m* reconnecting...\x1b[0m\x1b8")
                        await writer.drain()
                    except Exception:
                        pass
                    await asyncio.sleep(reconnect_delay)
                else:
                    logger.debug("ws_reconnect_exhausted: giving up after %d attempts", max_reconnects)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
