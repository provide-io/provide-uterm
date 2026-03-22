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
        data_parts: list[str] = []
        idx = 0

        while idx < len(self._buffer):
            current = self._buffer[idx]
            if current != DLE:
                data_parts.append(current)
                idx += 1
                continue

            if idx + 1 >= len(self._buffer):
                if final:
                    raise ControlStreamProtocolError("truncated control frame")
                break

            next_char = self._buffer[idx + 1]
            if next_char == DLE:
                data_parts.append(DLE)
                idx += 2
                continue
            if next_char != STX:
                raise ControlStreamProtocolError("invalid control prefix")

            if data_parts:
                events.append(DataChunk("".join(data_parts)))
                data_parts = []

            if len(self._buffer) - idx < _HEADER_BYTES:
                if final:
                    raise ControlStreamProtocolError("truncated control frame")
                break

            length_hex = self._buffer[idx + 2 : idx + 10]
            separator = self._buffer[idx + 10]
            if separator != ":" or any(char not in _HEX_DIGITS for char in length_hex):
                raise ControlStreamProtocolError("invalid control header")

            payload_bytes = int(length_hex, 16)
            if payload_bytes > self._max_control_payload_bytes:
                raise ControlStreamProtocolError("control payload too large")

            frame_end = idx + _HEADER_BYTES + payload_bytes
            if len(self._buffer) < frame_end:
                if final:
                    raise ControlStreamProtocolError("truncated control frame")
                break

            payload_raw = self._buffer[idx + _HEADER_BYTES : frame_end]
            try:
                payload = json.loads(payload_raw)
            except json.JSONDecodeError as exc:
                raise ControlStreamProtocolError("invalid control json") from exc
            if not isinstance(payload, dict):
                raise ControlStreamProtocolError("control payload must be an object")
            events.append(ControlChunk(payload))
            idx = frame_end

        if idx > 0:
            self._buffer = self._buffer[idx:]
        if data_parts:
            events.append(DataChunk("".join(data_parts)))
        return events
