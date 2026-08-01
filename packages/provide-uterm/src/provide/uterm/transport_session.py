#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""TransportSession — shared base for transport-backed terminal sessions.

Wraps any :class:`~provide.uterm.transports.base.ConnectionTransport`
together with a :class:`~provide.uterm.emulator.TerminalEmulator` to provide
a ready-to-use :class:`~provide.uterm.io.Session`-compliant object: it owns
the background reader loop, the screen-change sequence counter, the raw-byte
watcher fan-out, and the connect/close lifecycle.

Concrete subclasses (e.g.
:class:`~provide.uterm.telnet_session.TelnetSession` and
:class:`~provide.uterm.ws_session.WebSocketSession`) only need to:

- pass their transport instance and ``send_encoding`` to ``super().__init__``,
- override :meth:`_connect_transport` with the transport-specific connect
  call (it is the single transport-specific hook; the base raises
  :class:`NotImplementedError`).

Requires the ``emulator`` extra::

    pip install 'provide-uterm[emulator]'
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from provide.uterm.control_channel import ControlFrameDecoder, DataChunk
from provide.uterm.emulator import TerminalEmulator

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from provide.uterm.transports.base import ConnectionTransport


@dataclass(frozen=True, slots=True)
class TerminalFrame:
    """One terminal update with its correlated, owned screen snapshot."""

    sequence: int
    snapshot: dict[str, Any]
    transcript_delta: str

    @property
    def cursor(self) -> dict[str, Any]:
        """Return an owned copy of the frame's cursor position."""
        cursor = self.snapshot.get("cursor")
        return dict(cursor) if isinstance(cursor, dict) else {"x": 0, "y": 0}


class TerminalCapture:
    """Bounded terminal text captured only for one caller-owned operation."""

    def __init__(self, *, max_chars: int) -> None:
        self._max_chars = max(1, int(max_chars))
        self._text = ""

    @property
    def text(self) -> str:
        """Return terminal text received while the capture scope was active."""
        return self._text

    def _append(self, text: str) -> None:
        if text:
            self._text = (self._text + text)[-self._max_chars :]


class TransportSession:
    """Transport + pyte terminal emulation behind the Session protocol.

    Satisfies the :class:`~provide.uterm.io.Session` protocol:
    ``snapshot()``, ``send()``, ``wait_for_update()``.

    Args:
        transport: A connected-or-connectable
            :class:`~provide.uterm.transports.base.ConnectionTransport`.
        cols: Terminal width (default 80).
        rows: Terminal height (default 25).
        send_encoding: Codec used by :meth:`send` to encode outgoing strings
            (``"utf-8"`` by default; telnet uses ``"cp437"``). Encoding always
            uses ``errors="replace"`` so unrepresentable characters never raise.
        receive_encoding: Codec used by the emulator to decode incoming
            terminal bytes (``"cp437"`` by default).
    """

    def __init__(
        self,
        transport: ConnectionTransport,
        *,
        cols: int = 80,
        rows: int = 25,
        send_encoding: str = "utf-8",
        receive_encoding: str = "cp437",
        control_frames: bool = False,
    ) -> None:
        self._transport = transport
        self._cols = cols
        self._rows = rows
        self._send_encoding = send_encoding
        self._receive_encoding = receive_encoding
        self._emulator = TerminalEmulator(cols, rows, receive_encoding=receive_encoding)
        self._read_task: asyncio.Task[None] | None = None
        self._update_event = asyncio.Event()
        self._connected = False
        self._change_seq: int = 0
        self._terminal_frames: deque[TerminalFrame] = deque(maxlen=128)
        # Raw-byte watchers — called from ``_reader_loop`` after every
        # successful read with the IAC-stripped CP437+ANSI byte chunk.
        # Used by worker_term_bridge to fan terminal output (with colors
        # intact) to the swarm manager's hijack hub.
        self._watchers: list[Callable[[dict[str, Any], bytes], None]] = []
        # control_frames is OFF by default: every byte from the wire (even one
        # that happens to start with the DLE/STX control-frame magic bytes)
        # goes straight to the emulator/watchers unmodified, exactly as it
        # always has. Opt in to have inline DLE/STX control frames (e.g. a
        # server-emitted "render_speed" event) parsed out and routed to
        # control-frame watchers instead of appearing as literal text on the
        # rendered screen.
        self._control_frames = control_frames
        self._control_decoder: ControlFrameDecoder | None = ControlFrameDecoder() if control_frames else None
        self._control_watchers: list[Callable[[dict[str, Any]], None]] = []
        self._captures: list[TerminalCapture] = []

    async def _connect_transport(self) -> None:
        """Open the underlying transport. Override in subclasses.

        This is the single transport-specific hook. Subclasses call
        ``self._transport.connect(...)`` with their own argument shape
        (telnet passes host/port/cols/rows/term/timeout; WebSocket passes
        a ``url``). The base implementation raises so a misconfigured
        subclass fails loudly rather than silently connecting to nothing.
        """
        raise NotImplementedError("subclasses must implement _connect_transport")

    async def connect(self) -> None:
        """Open the transport connection and start the background reader."""
        await self._connect_transport()
        self._connected = True
        self._read_task = asyncio.create_task(self._reader_loop())

    async def close(self) -> None:
        """Close the connection and stop the background reader."""
        self._connected = False
        if self._read_task is not None:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._read_task
            self._read_task = None
        await self._transport.disconnect()

    async def __aenter__(self) -> TransportSession:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # ── Session protocol ──────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """Return the current emulated screen state."""
        return self._emulator.get_snapshot()

    def ansi_screen(self) -> str:
        """Return the current screen as ANSI-styled text (with SGR colors).

        Delegates to :meth:`TerminalEmulator.ansi_screen`. Use this when
        shipping a snapshot to a live renderer (xterm.js dashboard,
        AnsiBuffer spy) so colors survive — :meth:`snapshot` returns
        plain text only.
        """
        return self._emulator.ansi_screen()

    async def send(self, data: str) -> None:
        """Send a string to the server, encoded with the configured codec."""
        await self._transport.send(data.encode(self._send_encoding, errors="replace"))

    async def send_expect(
        self,
        keys: str,
        *,
        expect_text: str | None = None,
        expect_regex: str | None = None,
        timeout_ms: int = 5000,
        sanitize: bool = True,
    ) -> Any:
        """Send keys and wait for expected terminal output."""
        from provide.uterm.expect import send_and_expect

        return await send_and_expect(
            self,
            keys,
            expect_text=expect_text,
            expect_regex=expect_regex,
            timeout_ms=timeout_ms,
            sanitize=sanitize,
        )

    async def wait_for_update(self, *, timeout_ms: int, since: int | None = None) -> bool:
        """Wait until new bytes arrive from the server, or timeout.

        Args:
            timeout_ms: Maximum wait time in milliseconds.
            since: Ignored (kept for protocol compatibility).

        Returns:
            ``True`` if new data arrived, ``False`` on timeout.
        """
        self._update_event.clear()
        try:
            await asyncio.wait_for(self._update_event.wait(), timeout=timeout_ms / 1000.0)
            return True
        except TimeoutError:
            return False

    def is_connected(self) -> bool:
        """Return ``True`` if the session is connected."""
        return self._connected

    def screen_change_seq(self) -> int:
        """Return a monotonic counter that increments on each screen update.

        Capture this *before* sending input, then pass the value to
        :meth:`wait_for_screen_change` to avoid reading stale screen data.
        """
        return self._change_seq

    # Alias used by some callers.
    update_seq = screen_change_seq

    async def wait_for_screen_change(self, *, timeout_ms: int = 5000, since: int | None = None) -> bool:
        """Wait until the screen updates beyond *since*, or timeout.

        Args:
            timeout_ms: Maximum wait time in milliseconds.
            since: Sequence number from :meth:`screen_change_seq`.
                If ``None``, waits for any next update. (The Go port's
                ``WaitForScreenChange`` uses a negative ``since`` for this
                same "wait for any" case instead of an optional/pointer type —
                intentional, not a bug: it keeps a shared Go interface with
                multiple implementers simple.)

        Returns:
            ``True`` if the screen changed, ``False`` on timeout.
        """
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000.0
        while True:
            if since is not None and self._change_seq > since:
                return True
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return False
            self._update_event.clear()
            try:
                await asyncio.wait_for(self._update_event.wait(), timeout=remaining)
            except TimeoutError:
                return self._change_seq > (since or 0)

    async def wait_for_terminal_frame(self, *, since: int, timeout_ms: int) -> TerminalFrame | None:
        """Return the first correlated terminal frame newer than *since*."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000.0
        while True:
            frame = next((candidate for candidate in self._terminal_frames if candidate.sequence > since), None)
            if frame is not None:
                return frame

            remaining = deadline - loop.time()
            if remaining <= 0:
                return None

            self._update_event.clear()
            frame = next((candidate for candidate in self._terminal_frames if candidate.sequence > since), None)
            if frame is not None:
                return frame

            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._update_event.wait(), timeout=remaining)
            except TimeoutError:
                pass

    # ── Internal ──────────────────────────────────────────────────────────

    def add_watch(
        self,
        callback: Callable[[dict[str, Any], bytes], None],
        *,
        interval_s: float = 0.0,
    ) -> None:
        """Register a callback fired with each raw byte chunk read from the wire.

        This is the supported tap for raw terminal bytes on every transport
        session (telnet and websocket): register the watcher here to receive
        IAC-stripped, ANSI/CP437-intact bytes before pyte processes them.
        For examples, see :meth:`add_watch` and
        ``session.add_watch(lambda state, raw: buf.extend(raw))``.

        Called from ``_reader_loop`` immediately after IAC stripping and
        *before* the emulator processes the bytes — so the chunk still
        contains every ANSI SGR escape, cursor-positioning sequence and
        CP437 high byte that arrived from the server. Useful for fanning
        terminal output (with colors intact) to a hijack hub or
        recording tee, since :meth:`snapshot` returns pyte's plain-text
        decoded display which has already absorbed the escape sequences.

        Args:
            callback: ``(state_dict, raw_bytes) -> None``. ``state_dict``
                is currently always empty; the second positional carries
                the byte chunk. Callbacks must NOT block — schedule any
                async work onto a queue / task.
            interval_s: Reserved for future throttled-fan-out modes;
                currently ignored.
        """
        del interval_s  # reserved for compatibility with TermBridge variants
        self._watchers.append(callback)

    def add_control_frame_watch(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback fired with each inline control frame's payload.

        Only invoked when the session was constructed with
        ``control_frames=True``; a no-op registration otherwise (there is
        nothing to dispatch since the decoder is never engaged). The payload
        is the parsed JSON object (e.g. ``{"type": "render_speed", "cps": 2400}``)
        — the raw framing bytes never reach the emulator or :meth:`add_watch`
        callbacks in this mode.

        Args:
            callback: ``(control_payload) -> None``. Must not block.
        """
        self._control_watchers.append(callback)

    @contextlib.contextmanager
    def capture_output(self, *, max_chars: int = 65_536) -> Iterator[TerminalCapture]:
        """Capture terminal-only text received during this bounded scope.

        Control frames are removed before text reaches the capture. The
        capture unregisters on scope exit, so it cannot become session-wide
        history or grow with session age.
        """
        capture = TerminalCapture(max_chars=max_chars)
        self._captures.append(capture)
        try:
            yield capture
        finally:
            with contextlib.suppress(ValueError):
                self._captures.remove(capture)

    async def _reader_loop(self) -> None:
        """Background task: read from transport (IAC-stripped), feed into emulator."""
        try:
            while self._connected:
                raw = await self._transport.receive(4096, timeout_ms=500)
                if raw:
                    data: bytes | None = raw
                    if self._control_decoder is not None:
                        data = self._split_control_frames(raw)
                    if data is None:
                        continue
                    if self._captures:
                        terminal_text = data.decode(self._receive_encoding, errors="replace")
                        for capture in tuple(self._captures):
                            capture._append(terminal_text)
                    # Fan out to any registered watchers BEFORE the emulator
                    # consumes the bytes, so they see the raw wire content
                    # (ANSI SGR codes etc.) and not pyte's decoded display.
                    if self._watchers:
                        for cb in self._watchers:
                            with contextlib.suppress(Exception):
                                cb({}, data)
                    self._emulator.process(data)
                    self._change_seq += 1
                    snapshot = deepcopy(self._emulator.get_snapshot())
                    self._terminal_frames.append(
                        TerminalFrame(
                            sequence=self._change_seq,
                            snapshot=snapshot,
                            transcript_delta=data.decode(self._receive_encoding, errors="replace"),
                        )
                    )
                    self._update_event.set()
        except (asyncio.CancelledError, ConnectionResetError, OSError, ConnectionError):
            self._connected = False

    def _split_control_frames(self, data: bytes) -> bytes | None:
        """Run *data* through the control-frame decoder.

        Control chunks are dispatched to control-frame watchers; data chunks
        are re-joined and returned using the configured receive encoding.
        Returns ``None`` when
        the chunk contained only control frames (nothing left for the caller
        to feed onward this round).
        """
        assert self._control_decoder is not None  # guarded by the caller
        text = data.decode(self._receive_encoding, errors="replace")
        terminal_text_parts: list[str] = []
        for chunk in self._control_decoder.feed(text):
            if isinstance(chunk, DataChunk):
                terminal_text_parts.append(chunk.data)
            else:
                for cb in self._control_watchers:
                    with contextlib.suppress(Exception):
                        cb(chunk.control)
        if not terminal_text_parts:
            return None
        return "".join(terminal_text_parts).encode(self._receive_encoding, errors="replace")
