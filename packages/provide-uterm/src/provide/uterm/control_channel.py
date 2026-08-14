#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Inline DLE/STX control framing for mixed terminal data and control messages.

Control frame headers store the UTF-8 byte length of the JSON payload, not the
Python character count. ASCII payloads therefore keep their historical wire
shape while raw Unicode payloads interoperate with browser runtimes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from string import hexdigits
from typing import TYPE_CHECKING, Any, cast

try:
    import orjson  # type: ignore[import-not-found]  # ty:ignore[unresolved-import]

    def _json_dumps(obj: Any) -> str:
        return cast("str", orjson.dumps(obj).decode("utf-8"))

    _json_loads: Callable[[str], Any] = orjson.loads
except ImportError:
    try:
        import ujson  # type: ignore[import-untyped]  # ty:ignore[unresolved-import]

        def _json_dumps(obj: Any) -> str:
            return cast("str", ujson.dumps(obj, ensure_ascii=False))

        _json_loads = ujson.loads
    except ImportError:

        def _json_dumps(obj: Any) -> str:
            return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

        _json_loads = json.loads

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


DLE = "\x10"
STX = "\x02"
_DLE_BYTE = ord(DLE)
_STX_BYTE = ord(STX)
_HEADER_BYTES = 11  # DLE STX + 8 hex digits + ':'
_MAX_CONTROL_PAYLOAD_BYTES = 1_048_576
_DEFAULT_BUFFER_BYTES = 10_485_760
_HEX_DIGITS = frozenset(hexdigits)
_HEX_BYTE_DIGITS = frozenset(_byte.encode("ascii")[0] for _byte in hexdigits)
_DLE_STX = bytes((_DLE_BYTE, _STX_BYTE))
_DLE_STX_VIEW = memoryview(_DLE_STX)
# Maximum JSON nesting depth in a control frame. The 1 MB frame size limit
# bounds the *size* of a hostile payload, but a deeply nested structure
# like `[[[…]]]` of depth ~500 fits in well under 1 MB and would burn
# stack/CPU on every consumer that walks the decoded object. 32 is well
# above any legitimate control-frame shape (hello frames are flat dicts,
# the deepest field — annotations — is two layers).
_MAX_CONTROL_FRAME_DEPTH = 32


class ControlFrameProtocolError(ValueError):
    """Raised when an inline control frame is malformed."""


@dataclass(frozen=True, slots=True)
class DataChunk:
    """Decoded terminal data from the inline stream."""

    data: str

    @property
    def kind(self) -> str:
        return "data"


@dataclass(frozen=True, slots=True)
class ControlChunk:
    """Decoded control payload from the inline stream."""

    control: dict[str, Any]

    @property
    def kind(self) -> str:
        return "control"


ControlFrameChunk = DataChunk | ControlChunk


def encode_terminal_data(data: str) -> str:
    """Encode terminal data for the inline stream."""
    return data.replace(DLE, DLE + DLE)


def encode_control_frame(payload: Mapping[str, Any]) -> str:
    """Encode a control payload for the inline stream."""
    serialized = _json_dumps(dict(payload))
    return f"{DLE}{STX}{len(serialized.encode('utf-8')):08x}:{serialized}"


def _utf8_payload_end(buf: str, start: int, payload_bytes: int) -> int | None:
    """Return the character index ending a UTF-8 byte-length payload.

    Returns None when *buf* does not yet contain ``payload_bytes`` bytes from
    *start*. Raises :class:`ControlFrameProtocolError` when the declared byte
    length splits a Unicode code point (which appending more text cannot fix).
    """
    byte_count = 0
    idx = start
    while idx < len(buf) and byte_count < payload_bytes:
        byte_count += len(buf[idx].encode("utf-8"))
        idx += 1
        if byte_count > payload_bytes:
            raise ControlFrameProtocolError("invalid control payload length")
    if byte_count < payload_bytes:
        return None
    return idx


def is_control_frame(message: str) -> bool:
    """Return ``True`` when *message* is a full control-framed payload.

    The check is structural only: it validates the magic bytes, length
    header syntax, and that the declared UTF-8 payload bytes are fully
    present in the string.
    """
    if len(message) < _HEADER_BYTES:
        return False
    if not message.startswith(f"{DLE}{STX}"):
        return False
    if message[10] != ":":
        return False
    length_hex = message[2:10]
    if any(char not in _HEX_DIGITS for char in length_hex):
        return False
    payload_bytes = int(length_hex, 16)
    if f"{payload_bytes:08x}" != length_hex:
        return False
    if payload_bytes > _MAX_CONTROL_PAYLOAD_BYTES:
        return False

    try:
        payload_end = _utf8_payload_end(message, _HEADER_BYTES, payload_bytes)
    except ControlFrameProtocolError:
        return False

    return payload_end is not None and payload_end == len(message)


def _check_json_depth(value: Any, *, max_depth: int) -> None:
    """Raise ``ControlFrameProtocolError`` if ``value`` nests deeper than ``max_depth``.

    Walks the decoded JSON structure iteratively (no Python recursion) so a
    pathological payload cannot crash this check itself. Strings and
    primitive leaves count as depth-0; dicts and lists each add 1.
    """
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            raise ControlFrameProtocolError(f"control payload nests deeper than {max_depth}")
        if isinstance(node, dict):
            for child in node.values():
                if isinstance(child, (dict, list)):
                    stack.append((child, depth + 1))
        elif isinstance(node, list):
            for child in node:
                if isinstance(child, (dict, list)):
                    stack.append((child, depth + 1))


class ControlFrameDecoder:
    """Incrementally decode the inline DLE/STX control-frame stream."""

    def __init__(
        self,
        *,
        max_control_payload_bytes: int = _MAX_CONTROL_PAYLOAD_BYTES,
        max_buffer_bytes: int = _DEFAULT_BUFFER_BYTES,
        max_frame_depth: int = _MAX_CONTROL_FRAME_DEPTH,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._max_control_payload_bytes = max(1, int(max_control_payload_bytes))
        self._max_buffer_bytes = max(1, int(max_buffer_bytes))
        self._max_frame_depth = max(1, int(max_frame_depth))
        self._buffer = ""
        self._buffer_bytes = bytearray()
        self._buffer_parts: list[str] = []
        self._on_error = on_error

    def _report_error(self, message: str) -> ControlFrameProtocolError:
        if self._on_error:
            self._on_error("control_frame_protocol_error")
        return ControlFrameProtocolError(message)

    @staticmethod
    def _coerce_input(chunk: str) -> memoryview:
        """Return a byte view for parser operations."""
        if not isinstance(chunk, str):
            raise TypeError(f"control frame chunks must be str, got {type(chunk).__name__!r}")
        return memoryview(chunk.encode("utf-8"))

    @staticmethod
    def _decode_data_parts(parts: list[memoryview | bytes]) -> str:
        if not parts:
            return ""
        if len(parts) == 1:
            part = parts[0]
            if isinstance(part, memoryview):
                return part.tobytes().decode("utf-8")
            return part.decode("utf-8")

        merged = bytearray()
        for part in parts:
            merged.extend(part)
        return merged.decode("utf-8")

    def feed(self, chunk: str) -> list[ControlFrameChunk]:
        """Decode all complete events from *chunk* and buffer the rest."""
        chunk_view = self._coerce_input(chunk)
        self._buffer_bytes.extend(chunk_view)

        if len(self._buffer_bytes) > self._max_buffer_bytes:
            buffered_bytes = len(self._buffer_bytes)
            self._buffer = ""
            self._buffer_bytes = bytearray()
            self._buffer_parts = []
            raise self._report_error(f"control frame buffer overflow: {buffered_bytes} > {self._max_buffer_bytes}")

        try:
            events = self._drain(final=False)
        except ControlFrameProtocolError:
            self._buffer = ""
            self._buffer_bytes = bytearray()
            self._buffer_parts = []
            raise
        return events

    def finish(self) -> list[ControlFrameChunk]:
        """Decode any remaining buffered data and reject truncated control frames."""
        try:
            events = self._drain(final=True)
            if self._buffer or self._buffer_bytes:
                self._buffer = ""
                self._buffer_parts = []
                self._buffer_bytes = bytearray()
                raise self._report_error("truncated control frame")
        except ControlFrameProtocolError:
            self._buffer = ""
            self._buffer_parts = []
            self._buffer_bytes = bytearray()
            raise
        return events

    def _parse_frame_payload(self, payload_raw: str) -> dict[str, Any]:
        """Parse and validate a control frame JSON payload."""
        try:
            payload = _json_loads(payload_raw)
        except Exception as exc:
            raise self._report_error("invalid control json") from exc
        if not isinstance(payload, dict):
            raise self._report_error("control payload must be an object")
        try:
            _check_json_depth(payload, max_depth=self._max_frame_depth)
        except ControlFrameProtocolError as exc:
            # ``_report_error`` already fires ``self._on_error(...)``; an inline
            # notification here would double-fire the callback for one error
            # (and only adds dead, behaviourally-inert mutable literals).
            raise self._report_error(str(exc)) from exc
        return payload

    def _payload_end_for_utf8_length(
        self, buf: bytes | bytearray | memoryview, start: int, payload_bytes: int
    ) -> tuple[int, str] | None:
        """Locate the payload end via the shared helper, firing the decoder's
        error hook when the declared byte length splits a Unicode code point."""
        if len(buf) < start + payload_bytes:
            return None
        payload = bytes(buf[start : start + payload_bytes])
        try:
            payload_raw = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise self._report_error("invalid control payload length") from exc
        return start + payload_bytes, payload_raw

    def _try_parse_frame(
        self, buf: bytes | bytearray | memoryview, idx: int, buf_len: int, *, final: bool
    ) -> tuple[ControlChunk, int] | None:
        """Parse a control frame at buf[idx]. Returns (chunk, frame_end) or None if incomplete.

        Raises ControlFrameProtocolError on protocol violations.
        Returns None when the frame is not yet complete (only valid when final=False).
        """
        if buf_len - idx < _HEADER_BYTES:
            if final:
                raise self._report_error("truncated control frame")
            return None

        length_hex = bytes(buf[idx + 2 : idx + 10])
        separator = buf[idx + 10]
        if separator != ord(":") or any(byte not in _HEX_BYTE_DIGITS for byte in length_hex):
            raise self._report_error("invalid control header")
        payload_bytes = int(length_hex.decode("ascii"), 16)

        # Bounding check: frames > 1MB are rejected immediately before allocation
        if payload_bytes > _MAX_CONTROL_PAYLOAD_BYTES or payload_bytes > self._max_control_payload_bytes:
            raise self._report_error("control payload too large")

        payload_start = idx + _HEADER_BYTES
        end_and_payload = self._payload_end_for_utf8_length(buf, payload_start, payload_bytes)
        if end_and_payload is None:
            if final:
                raise self._report_error("truncated control frame")
            return None
        frame_end, payload_raw = end_and_payload
        return ControlChunk(self._parse_frame_payload(payload_raw)), frame_end

    @staticmethod
    def _emit_data_chunk(
        events: list[ControlFrameChunk],
        data_parts: list[memoryview],
        buf: bytes | bytearray | memoryview,
        data_start: int,
        idx: int,
    ) -> int:
        """Emit accumulated plain data as a DataChunk. Returns new data_start (= idx)."""
        if data_start < idx:
            data_parts.append(buf[data_start:idx])
        if data_parts:
            events.append(DataChunk(ControlFrameDecoder._decode_data_parts(data_parts)))
            data_parts.clear()
        return idx

    @staticmethod
    def _flush_remaining(
        buf: bytes | bytearray | memoryview,
        idx: int,
        data_start: int,
        data_parts: list[memoryview],
        events: list[ControlFrameChunk],
    ) -> int:
        """Flush unconsumed buffer tail and any trailing plain data.

        Returns the next parse offset.
        """
        if data_start < idx:
            data_parts.append(buf[data_start:idx])
        if data_parts:
            events.append(DataChunk(ControlFrameDecoder._decode_data_parts(data_parts)))
        return idx

    def _drain(self, *, final: bool) -> list[ControlFrameChunk]:
        events: list[ControlFrameChunk] = []
        if not self._buffer_bytes:
            return events

        buf = memoryview(self._buffer_bytes)
        buf_len = len(buf)
        idx = 0
        # Accumulate plain data parts (slices + escaped DLEs) to join later.
        data_parts: list[memoryview] = []
        data_start = 0  # start of current plain-data slice

        while idx < buf_len:
            if buf[idx] != _DLE_BYTE:
                idx += 1
                continue

            if idx + 1 >= buf_len:
                if final:
                    raise self._report_error("truncated control frame")
                break

            next_byte = buf[idx + 1]
            if next_byte == _DLE_BYTE:
                # Escaped DLE: save data slice before it, add literal DLE
                if data_start < idx:
                    data_parts.append(buf[data_start:idx])
                data_parts.append(_DLE_STX_VIEW[0:1])  # one-byte literal DLE
                idx += 2
                data_start = idx
                continue
            if next_byte != _STX_BYTE:
                raise self._report_error("invalid control prefix")

            data_start = self._emit_data_chunk(events, data_parts, buf, data_start, idx)

            result = self._try_parse_frame(buf, idx, buf_len, final=final)
            if result is None:
                break
            chunk, idx = result
            data_start = idx
            events.append(chunk)

        next_offset = self._flush_remaining(buf, idx, data_start, data_parts, events)
        if next_offset >= len(self._buffer_bytes):
            self._buffer = ""
            self._buffer_bytes = bytearray()
            self._buffer_parts = []
        else:
            self._buffer_bytes = bytearray(buf[next_offset:])
            self._buffer = self._buffer_bytes.decode("utf-8")
            self._buffer_parts = [self._buffer]
        return events
