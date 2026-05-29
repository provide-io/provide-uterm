#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Inline control channel framing for mixed terminal data and control messages.

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
_HEADER_BYTES = 11  # DLE STX + 8 hex digits + ':'
_MAX_CONTROL_PAYLOAD_BYTES = 1_048_576
_DEFAULT_BUFFER_BYTES = 10_485_760
_HEX_DIGITS = frozenset(hexdigits)
# Maximum JSON nesting depth in a control frame. The 1 MB frame size limit
# bounds the *size* of a hostile payload, but a deeply nested structure
# like `[[[…]]]` of depth ~500 fits in well under 1 MB and would burn
# stack/CPU on every consumer that walks the decoded object. 32 is well
# above any legitimate control-frame shape (hello frames are flat dicts,
# the deepest field — annotations — is two layers).
_MAX_CONTROL_FRAME_DEPTH = 32


class ControlChannelProtocolError(ValueError):
    """Raised when an inline control channel chunk is malformed."""


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


ControlChannelChunk = DataChunk | ControlChunk


def encode_data(data: str) -> str:
    """Encode terminal data for the inline stream."""
    return data.replace(DLE, DLE + DLE)


def encode_control(payload: Mapping[str, Any]) -> str:
    """Encode a control payload for the inline stream."""
    serialized = _json_dumps(dict(payload))
    return f"{DLE}{STX}{len(serialized.encode('utf-8')):08x}:{serialized}"


def _check_json_depth(value: Any, *, max_depth: int) -> None:
    """Raise ``ControlChannelProtocolError`` if ``value`` nests deeper than ``max_depth``.

    Walks the decoded JSON structure iteratively (no Python recursion) so a
    pathological payload cannot crash this check itself. Strings and
    primitive leaves count as depth-0; dicts and lists each add 1.
    """
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            raise ControlChannelProtocolError(f"control payload nests deeper than {max_depth}")
        if isinstance(node, dict):
            for child in node.values():
                if isinstance(child, (dict, list)):
                    stack.append((child, depth + 1))
        elif isinstance(node, list):
            for child in node:
                if isinstance(child, (dict, list)):
                    stack.append((child, depth + 1))


class ControlChannelDecoder:
    """Incrementally decode the inline control channel."""

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
        self._buffer_parts: list[str] = []
        self._on_error = on_error

    def _report_error(self, message: str) -> ControlChannelProtocolError:
        if self._on_error:
            self._on_error("control_channel_protocol_error")
        return ControlChannelProtocolError(message)

    def feed(self, chunk: str) -> list[ControlChannelChunk]:
        """Decode all complete events from *chunk* and buffer the rest."""
        if not isinstance(chunk, str):
            raise TypeError(f"control channel chunks must be str, got {type(chunk).__name__!r}")
        self._buffer_parts.append(chunk)
        total = sum(len(p.encode("utf-8")) for p in self._buffer_parts)
        if total > self._max_buffer_bytes:
            self._buffer_parts.clear()
            self._buffer = ""
            raise self._report_error(f"control channel buffer overflow: {total} > {self._max_buffer_bytes}")
        self._buffer = "".join(self._buffer_parts)
        try:
            events = self._drain(final=False)  # pragma: no mutate
        except ControlChannelProtocolError:
            self._buffer_parts.clear()
            self._buffer = ""
            raise
        # After _drain, self._buffer contains only unconsumed data.
        # Rebuild _buffer_parts with the unconsumed portion.
        self._buffer_parts = [self._buffer] if self._buffer else []
        return events

    def finish(self) -> list[ControlChannelChunk]:
        """Decode any remaining buffered data and reject truncated control frames."""
        try:
            events = self._drain(final=True)  # pragma: no mutate
        except ControlChannelProtocolError:
            self._buffer_parts.clear()
            self._buffer = ""
            raise
        if self._buffer:
            self._buffer_parts.clear()
            self._buffer = ""
            raise self._report_error("truncated control frame")
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
        except ControlChannelProtocolError as exc:
            # ``_report_error`` already fires ``self._on_error(...)``; an inline
            # notification here would double-fire the callback for one error
            # (and only adds dead, behaviourally-inert mutable literals).
            raise self._report_error(str(exc)) from exc
        return payload

    def _payload_end_for_utf8_length(self, buf: str, start: int, payload_bytes: int) -> int | None:
        """Return the character index ending a UTF-8 byte-length payload.

        Returns None when the current buffer does not yet contain enough bytes.
        Raises when the declared byte length splits a Unicode code point, which
        cannot become valid by appending more text to this str buffer.
        """
        byte_count = 0
        idx = start
        while idx < len(buf) and byte_count < payload_bytes:
            byte_count += len(buf[idx].encode("utf-8"))
            idx += 1
            if byte_count > payload_bytes:
                raise self._report_error("invalid control payload length")
        if byte_count < payload_bytes:
            return None
        return idx

    def _try_parse_frame(self, buf: str, idx: int, buf_len: int, *, final: bool) -> tuple[ControlChunk, int] | None:
        """Parse a control frame at buf[idx]. Returns (chunk, frame_end) or None if incomplete.

        Raises ControlChannelProtocolError on protocol violations.
        Returns None when the frame is not yet complete (only valid when final=False).
        """
        if buf_len - idx < _HEADER_BYTES:
            if final:
                raise self._report_error("truncated control frame")
            return None
        length_hex = buf[idx + 2 : idx + 10]  # pragma: no mutate
        separator = buf[idx + 10]
        if separator != ":" or any(char not in _HEX_DIGITS for char in length_hex):
            raise self._report_error("invalid control header")
        payload_bytes = int(length_hex, 16)
        # Bounding check: frames > 1MB are rejected immediately before allocation
        if payload_bytes > _MAX_CONTROL_PAYLOAD_BYTES or payload_bytes > self._max_control_payload_bytes:
            raise self._report_error("control payload too large")
        payload_start = idx + _HEADER_BYTES
        frame_end = self._payload_end_for_utf8_length(buf, payload_start, payload_bytes)
        if frame_end is None:
            if final:
                raise self._report_error("truncated control frame")
            return None
        payload_raw = buf[payload_start:frame_end]
        return ControlChunk(self._parse_frame_payload(payload_raw)), frame_end

    @staticmethod
    def _emit_data_chunk(
        events: list[ControlChannelChunk],
        data_parts: list[str],
        buf: str,
        data_start: int,
        idx: int,
    ) -> int:
        """Emit accumulated plain data as a DataChunk. Returns new data_start (= idx)."""
        if data_start < idx:
            data_parts.append(buf[data_start:idx])
        if data_parts:
            events.append(DataChunk("".join(data_parts)))
            data_parts.clear()
        return idx

    def _flush_remaining(
        self,
        buf: str,
        idx: int,
        data_start: int,
        data_parts: list[str],
        events: list[ControlChannelChunk],
    ) -> None:
        """Flush unconsumed buffer tail and any trailing plain data."""
        if idx > 0:  # pragma: no mutate
            self._buffer = buf[idx:]
        if data_start < idx:
            data_parts.append(buf[data_start:idx])
        if data_parts:
            events.append(DataChunk("".join(data_parts)))

    def _drain(self, *, final: bool) -> list[ControlChannelChunk]:
        events: list[ControlChannelChunk] = []
        buf = self._buffer
        buf_len = len(buf)
        idx = 0
        # Accumulate plain data parts (slices + escaped DLEs) to join later.
        data_parts: list[str] = []
        data_start = 0  # start of current plain-data slice

        while idx < buf_len:
            if buf[idx] != DLE:
                idx += 1  # pragma: no mutate
                continue

            if idx + 1 >= buf_len:
                if final:
                    raise self._report_error("truncated control frame")
                break

            next_char = buf[idx + 1]
            if next_char == DLE:
                # Escaped DLE: save data slice before it, add literal DLE
                if data_start < idx:  # pragma: no cover — edge case: DLE at buffer boundary  # pragma: no mutate
                    data_parts.append(buf[data_start:idx])
                data_parts.append(DLE)
                idx += 2  # pragma: no mutate
                data_start = idx
                continue
            if next_char != STX:
                raise self._report_error("invalid control prefix")

            data_start = self._emit_data_chunk(events, data_parts, buf, data_start, idx)

            result = self._try_parse_frame(buf, idx, buf_len, final=final)  # pragma: no mutate
            if result is None:
                break
            chunk, idx = result
            data_start = idx
            events.append(chunk)

        self._flush_remaining(buf, idx, data_start, data_parts, events)
        return events
