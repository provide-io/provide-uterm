#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Gateway classes: TelnetWsGateway and SshWsGateway."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import collections.abc
    import ssl as _ssl
    from pathlib import Path

from provide.telemetry import get_logger
from provide.uterm.colors import ColorMode, apply_color_mode
from provide.uterm.control_channel import (
    ControlChannelDecoder,
    ControlChannelProtocolError,
    ControlChunk,
    DataChunk,
    encode_control,
    encode_data,
)
from provide.uterm.gateway._iac_negotiate import IacNegotiator

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Token file helpers
# ---------------------------------------------------------------------------


def _read_token(path: Path) -> dict[str, Any] | None:
    """Read a persisted token record from disk.

    The file is JSON: ``{"token": "...", "player_id": 42}``. ``player_id`` is
    optional — absent when the upstream server issues tokens that aren't
    keyed by player id. Returns ``None`` if the file is missing, empty, or
    unparseable (stale format, partial write, etc).
    """
    try:
        raw = path.read_text().strip()
    except (FileNotFoundError, OSError):
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Legacy bare-token file — keep it working so a proxy upgrade doesn't
        # force the user to re-login. Normalise into the new dict shape.
        return {"token": raw}
    if not isinstance(data, dict) or not data.get("token"):
        return None
    return data


def _write_token(path: Path, token: str, player_id: int | None = None) -> None:
    """Persist a token record to disk with 0600 file / 0700 parent perms."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)
    payload: dict[str, object] = {"token": token}
    if player_id is not None:
        payload["player_id"] = player_id
    path.write_text(json.dumps(payload))
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def _delete_token(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


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
    token_holder: list[dict[str, Any] | None],
    write_fn: collections.abc.Callable[[bytes], collections.abc.Coroutine[object, object, None]],
    *,
    token_file: Path | None = None,
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
        return await _handle_ws_control_frame(data, token_holder, write_fn, token_file=token_file)

    if not events:
        return False
    handled = False
    for event in events:
        if isinstance(event, DataChunk):
            return False
        handled = (
            await _handle_ws_control_frame(event.control, token_holder, write_fn, token_file=token_file) or handled
        )
    return handled


async def _handle_ws_control_frame(
    data: dict[str, object],
    token_holder: list[dict[str, Any] | None],
    write_fn: collections.abc.Callable[[bytes], collections.abc.Coroutine[object, object, None]],
    *,
    token_file: Path | None = None,
) -> bool:
    try:
        msg_type = data.get("type") if isinstance(data.get("type"), str) else None
    except AttributeError:
        return False
    if msg_type == "session_token" and "token" in data:
        pid = data.get("player_id")
        token_str = str(data["token"])
        token_dict: dict[str, object] = {"token": token_str}
        if isinstance(pid, int):
            token_dict["player_id"] = pid
        token_holder[0] = token_dict
        if token_file is not None:
            with contextlib.suppress(OSError):
                _write_token(token_file, token_str, pid if isinstance(pid, int) else None)
        return True
    if msg_type == "resume_ok":
        await write_fn(b"\r\n[Session resumed]\r\n")
        return True
    if msg_type == "resume_failed":
        token_holder[0] = None
        if token_file is not None:
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
        raise ImportError("websockets is required for gateway support: pip install 'provide-uterm[cli]'") from exc


# ---------------------------------------------------------------------------
# Shared pump helpers
# ---------------------------------------------------------------------------


async def _tcp_to_ws(
    reader: asyncio.StreamReader,
    ws: object,
    *,
    telnet: bool = False,
    negotiator: IacNegotiator | None = None,
    writer: asyncio.StreamWriter | None = None,
) -> None:
    """Forward raw TCP bytes → WebSocket text frames.

    When a ``negotiator`` is supplied, its :meth:`feed` handles IAC stripping
    *and* surfaces any replies the gateway should echo back to the client —
    e.g. the late ``IAC SB TTYPE SEND IAC SE`` replies that come in after a
    slow client catches up on negotiation. Without a negotiator the
    function falls back to the stateless ``_strip_iac`` path for backwards
    compatibility.
    """
    while True:
        data = await reader.read(4096)
        if not data:
            break
        if telnet:
            if negotiator is not None:  # pragma: no cover — IAC reply path runs only inside a live telnet session
                reply, cleaned = negotiator.feed(data)
                if reply and writer is not None:
                    writer.write(reply)
                    await writer.drain()
                data = cleaned
            else:
                data = _strip_iac(data)
            if not data:
                continue
        await ws.send(encode_data(data.decode("latin-1", errors="replace")))  # type: ignore[attr-defined]


async def _ws_to_tcp(
    ws: object,
    writer: asyncio.StreamWriter,
    *,
    token_holder: list[dict[str, Any] | None],
    color_mode: ColorMode = "passthrough",
    token_file: Path | None = None,
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
                    await _handle_ws_control_frame(event.control, token_holder, _write_fn, token_file=token_file)
                    continue
                raw = event.data.encode("latin-1", errors="replace")
                raw = raw.replace(b"\x7f", b"\x08")  # DEL→BS
                raw = _normalize_crlf(raw)
                raw = apply_color_mode(raw, color_mode)
                writer.write(raw)
                await writer.drain()
            continue
        raw = message
        raw = raw.replace(b"\x7f", b"\x08")  # DEL→BS
        raw = _normalize_crlf(raw)
        raw = apply_color_mode(raw, color_mode)
        writer.write(raw)
        await writer.drain()


async def _pipe_ws(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    ws_url: str,
    *,
    token_holder: list[dict[str, Any] | None],
    color_mode: ColorMode = "passthrough",
    telnet: bool = False,
    token_file: Path | None = None,
    iac_negotiate: bool = False,
    iac_negotiate_timeout: float = 0.4,
    ws_ssl: _ssl.SSLContext | bool | None = None,
) -> None:
    """Open a WebSocket to *ws_url* and bidirectionally pipe with reader/writer.

    When ``iac_negotiate`` is True and ``telnet`` is True, the gateway
    performs a brief RFC 1091 TTYPE + RFC 1572 NEW-ENVIRON negotiation
    with the TCP client before opening the upstream WebSocket. Any
    detected colour-palette hint is appended to the WS URL as
    ``?colormode=…`` so the uterm-side auto-negotiator picks the
    correct palette from the very first byte of the welcome banner.
    """
    import websockets

    negotiator: IacNegotiator | None = None
    if telnet and iac_negotiate:
        negotiator = IacNegotiator()
        # Initial DO TTYPE / DO NEW-ENVIRON — kicks the client into replying.
        writer.write(negotiator.start_bytes())
        await writer.drain()
        # Give the client a narrow window to respond. Most modern telnet
        # clients (iTerm2, macOS telnet, Windows telnet) reply in <50ms;
        # 400ms is enough to cover slower links without adding perceptible
        # latency for the common fast case.
        deadline = asyncio.get_event_loop().time() + iac_negotiate_timeout
        while (
            not negotiator.done()
        ):  # pragma: no cover — IAC negotiation timing covered by live-telnet integration tests
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=remaining)
            except TimeoutError:
                break
            if not chunk:
                break
            reply, _cleaned = negotiator.feed(chunk)
            # Ignore any cleaned application bytes that arrived during
            # the pre-negotiation window — clients shouldn't send real
            # input before receiving the welcome banner. The negotiator
            # stays live for the duration of the session, so any late
            # IAC arrivals keep getting parsed correctly.
            if reply:
                writer.write(reply)
                await writer.drain()
        derived = negotiator.derived_colormode()
        if derived:
            sep = "&" if "?" in ws_url else "?"
            ws_url = f"{ws_url}{sep}colormode={derived}"
            logger.info(
                "telnet_colormode_negotiated",
                derived=derived,
                term=negotiator.term,
                colorterm=negotiator.env.get("COLORTERM", ""),
            )

    connect_kwargs: dict[str, object] = {}
    if ws_ssl is not None:
        connect_kwargs["ssl"] = ws_ssl

    async with websockets.connect(ws_url, **connect_kwargs) as ws:  # type: ignore[arg-type]
        token_data = token_holder[0]
        if token_data:
            resume_msg: dict[str, object] = {"type": "resume", "token": token_data["token"]}
            if "player_id" in token_data:
                resume_msg["player_id"] = token_data["player_id"]
            await ws.send(encode_control(resume_msg))
        t1 = asyncio.create_task(_tcp_to_ws(reader, ws, telnet=telnet, negotiator=negotiator, writer=writer))
        t2 = asyncio.create_task(
            _ws_to_tcp(ws, writer, token_holder=token_holder, color_mode=color_mode, token_file=token_file)
        )
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
    token_holder: list[dict[str, Any] | None],
    color_mode: ColorMode = "passthrough",
    token_file: Path | None = None,
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
                    await _handle_ws_control_frame(event.control, token_holder, _write_fn, token_file=token_file)
                    continue
                raw = event.data.encode("latin-1", errors="replace")
                raw = apply_color_mode(raw, color_mode)
                stdout.write(raw.decode("latin-1", errors="replace"))
        else:
            raw = apply_color_mode(message, color_mode)
            stdout.write(raw.decode("latin-1", errors="replace"))
