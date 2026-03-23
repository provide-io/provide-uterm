#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Inline control stream framing for mixed terminal data and control messages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from string import hexdigits
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

DLE = "\x10"
STX = "\x02"
_HEADER_BYTES = 11  # DLE STX + 8 hex digits + ':'
_HEX_DIGITS = frozenset(hexdigits)


class ControlStreamProtocolError(ValueError):
    """Raised when an inline control stream chunk is malformed."""


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


ControlStreamChunk = DataChunk | ControlChunk


def encode_data(data: str) -> str:
    """Encode terminal data for the inline stream."""
    return data.replace(DLE, DLE + DLE)


def encode_control(payload: Mapping[str, Any]) -> str:
    """Encode a control payload for the inline stream."""
    serialized = json.dumps(dict(payload), ensure_ascii=True, separators=(",", ":"))
    return f"{DLE}{STX}{len(serialized):08x}:{serialized}"


class ControlStreamDecoder:
    """Incrementally decode the inline control stream."""

    def __init__(self, *, max_control_payload_bytes: int = 1_048_576) -> None:
        self._max_control_payload_bytes = max(1, int(max_control_payload_bytes))
        self._buffer = ""
        self._buffer_parts: list[str] = []

    def feed(self, chunk: str) -> list[ControlStreamChunk]:
        """Decode all complete events from *chunk* and buffer the rest."""
        if not isinstance(chunk, str):
            raise TypeError(f"control stream chunks must be str, got {type(chunk).__name__!r}")
        self._buffer_parts.append(chunk)
        self._buffer = "".join(self._buffer_parts)
        events = self._drain(final=False)
        # After _drain, self._buffer contains only unconsumed data.
        # Rebuild _buffer_parts with the unconsumed portion.
        self._buffer_parts = [self._buffer] if self._buffer else []
        return events

    def finish(self) -> list[ControlStreamChunk]:
        """Decode any remaining buffered data and reject truncated control frames."""
        events = self._drain(final=True)
        if self._buffer:
            raise ControlStreamProtocolError("truncated control frame")
        return events

    def _drain(self, *, final: bool) -> list[ControlStreamChunk]:
        events: list[ControlStreamChunk] = []
        buf = self._buffer
        buf_len = len(buf)
        idx = 0
        # Accumulate plain data parts (slices + escaped DLEs) to join later.
        data_parts: list[str] = []
        data_start = 0  # start of current plain-data slice

        def _flush_data() -> None:
            """Emit accumulated plain data as a single DataChunk."""
            nonlocal data_start
            if data_start < idx:
                data_parts.append(buf[data_start:idx])
            if data_parts:
                events.append(DataChunk("".join(data_parts)))
                data_parts.clear()
            data_start = idx

        while idx < buf_len:
            if buf[idx] != DLE:
                idx += 1
                continue

            if idx + 1 >= buf_len:
                if final:
                    raise ControlStreamProtocolError("truncated control frame")
                break

            next_char = buf[idx + 1]
            if next_char == DLE:
                # Escaped DLE: save data slice before it, add literal DLE
                if data_start < idx:  # pragma: no cover — edge case: DLE at buffer boundary
                    data_parts.append(buf[data_start:idx])
                data_parts.append(DLE)
                idx += 2
                data_start = idx
                continue
            if next_char != STX:
                raise ControlStreamProtocolError("invalid control prefix")

            _flush_data()

            if buf_len - idx < _HEADER_BYTES:
                if final:
                    raise ControlStreamProtocolError("truncated control frame")
                break

            length_hex = buf[idx + 2 : idx + 10]
            separator = buf[idx + 10]
            if separator != ":" or any(char not in _HEX_DIGITS for char in length_hex):
                raise ControlStreamProtocolError("invalid control header")

            payload_bytes = int(length_hex, 16)
            if payload_bytes > self._max_control_payload_bytes:
                raise ControlStreamProtocolError("control payload too large")

            frame_end = idx + _HEADER_BYTES + payload_bytes
            if buf_len < frame_end:
                if final:
                    raise ControlStreamProtocolError("truncated control frame")
                break

            payload_raw = buf[idx + _HEADER_BYTES : frame_end]
            try:
                payload = json.loads(payload_raw)
            except json.JSONDecodeError as exc:
                raise ControlStreamProtocolError("invalid control json") from exc
            if not isinstance(payload, dict):
                raise ControlStreamProtocolError("control payload must be an object")
            events.append(ControlChunk(payload))
            idx = frame_end
            data_start = idx

        if idx > 0:
            self._buffer = buf[idx:]
        # Emit trailing plain data.
        if data_start < idx:
            data_parts.append(buf[data_start:idx])
        if data_parts:
            events.append(DataChunk("".join(data_parts)))
        return events
