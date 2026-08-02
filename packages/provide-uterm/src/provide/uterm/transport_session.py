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
from sys import getsizeof
from typing import TYPE_CHECKING, Any

from provide.uterm.terminal_frames import TerminalFrameDisconnectedError

from provide.uterm.control_channel import ControlFrameDecoder, DataChunk
from provide.uterm.emulator import TerminalEmulator

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from provide.uterm.transports.base import ConnectionTransport

# Frame history is bounded by both update count and an estimated retained-byte
# total. The newest frame is always kept complete even when it alone exceeds
# the byte budget; older frames are evicted first and sequence gaps are valid.
TERMINAL_FRAME_HISTORY_MAX_COUNT = 128
TERMINAL_FRAME_HISTORY_MAX_BYTES = 1_048_576
# Frame waits accept at most 24 hours, keeping deadline conversion bounded.
TERMINAL_FRAME_WAIT_MAX_MS = 86_400_000


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


def _retained_size(value: Any, seen: set[int]) -> int:
    """Estimate one owned JSON-like value's recursively retained bytes."""
    object_id = id(value)
    if object_id in seen:
        return 0
    seen.add(object_id)

    size = getsizeof(value)
    if isinstance(value, dict):
        return size + sum(_retained_size(key, seen) + _retained_size(item, seen) for key, item in value.items())
    if isinstance(value, (list, tuple, set, frozenset, deque)):
        return size + sum(_retained_size(item, seen) for item in value)
    return size


def _terminal_frame_size(frame: TerminalFrame) -> int:
    seen: set[int] = set()
    return (
        getsizeof(frame)
        + _retained_size(frame.sequence, seen)
        + _retained_size(frame.snapshot, seen)
        + _retained_size(frame.transcript_delta, seen)
    )


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
        self._terminal_frame_event = asyncio.Event()
        self._terminal_frame_closed = False
        self._connected = False
        self._change_seq: int = 0
        self._terminal_frames: deque[TerminalFrame] = deque(maxlen=TERMINAL_FRAME_HISTORY_MAX_COUNT)
        self._terminal_frame_sizes: deque[int] = deque(maxlen=TERMINAL_FRAME_HISTORY_MAX_COUNT)
        self._terminal_frame_bytes = 0
        self._terminal_frame_max_bytes = TERMINAL_FRAME_HISTORY_MAX_BYTES
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
        self._terminal_frame_closed = False
        self._read_task = asyncio.create_task(self._reader_loop())

    async def close(self) -> None:
        """Close the connection and stop the background reader."""
        self._connected = False
        self._terminal_frame_closed = True
        self._notify_terminal_frame_waiters()
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
        """Return the first correlated terminal frame newer than *since*.

        Non-positive timeouts perform one queue check without waiting. Values
        above :data:`TERMINAL_FRAME_WAIT_MAX_MS` raise :class:`ValueError`.
        History is bounded, so an old *since* may return the oldest retained
        frame with a sequence greater than ``since + 1`` after eviction. The
        newest frame is always retained complete; if it alone exceeds the
        nominal byte budget, the retained total may temporarily exceed it.

        Raises:
            TerminalFrameDisconnectedError: If the transport closes before a newer
                retained frame is available. A deadline timeout still returns
                ``None``.
        """
        if timeout_ms > TERMINAL_FRAME_WAIT_MAX_MS:
            raise ValueError(f"timeout_ms must be <= {TERMINAL_FRAME_WAIT_MAX_MS}")
        if timeout_ms <= 0:
            frame = next((candidate for candidate in self._terminal_frames if candidate.sequence > since), None)
            if frame is not None:
                return deepcopy(frame)
            if self._terminal_frame_closed:
                raise TerminalFrameDisconnectedError("terminal transport disconnected")
            return None

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000.0
        while True:
            notifier = self._terminal_frame_event
            frame = next((candidate for candidate in self._terminal_frames if candidate.sequence > since), None)
            if frame is not None:
                return deepcopy(frame)
            if self._terminal_frame_closed:
                raise TerminalFrameDisconnectedError("terminal transport disconnected")

            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(notifier.wait(), timeout=remaining)
            except TimeoutError:
                pass

    # ── Internal ──────────────────────────────────────────────────────────

    def _notify_terminal_frame_waiters(self) -> None:
        notifier = self._terminal_frame_event
        self._terminal_frame_event = asyncio.Event()
        notifier.set()

    def _append_terminal_frame(self, frame: TerminalFrame) -> None:
        """Append *frame* and evict old history by count and retained bytes."""
        frame_size = _terminal_frame_size(frame)
        if len(self._terminal_frames) == TERMINAL_FRAME_HISTORY_MAX_COUNT:
            self._terminal_frames.popleft()
            self._terminal_frame_bytes -= self._terminal_frame_sizes.popleft()
        self._terminal_frames.append(frame)
        self._terminal_frame_sizes.append(frame_size)
        self._terminal_frame_bytes += frame_size
        while len(self._terminal_frames) > 1 and self._terminal_frame_bytes > self._terminal_frame_max_bytes:
            self._terminal_frames.popleft()
            self._terminal_frame_bytes -= self._terminal_frame_sizes.popleft()

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
                    self._append_terminal_frame(
                        TerminalFrame(
                            sequence=self._change_seq,
                            snapshot=snapshot,
                            transcript_delta=data.decode(self._receive_encoding, errors="replace"),
                        )
                    )
                    self._notify_terminal_frame_waiters()
                    self._update_event.set()
        except (asyncio.CancelledError, ConnectionResetError, OSError, ConnectionError):
            pass
        finally:
            self._connected = False
            self._terminal_frame_closed = True
            self._notify_terminal_frame_waiters()

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
