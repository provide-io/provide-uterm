#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generic typed-channel negotiation over the inline control channel."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from provide.uterm.control_channel import ControlChunk, ControlFrameDecoder, is_control_frame


@dataclass(frozen=True, slots=True)
class ChannelHello:
    """Client-advertised typed-channel versions."""

    channels: dict[str, int]


class NegotiatedChannels:
    """Per-connection typed-channel grants and sequence counters."""

    def __init__(self, supported: Mapping[str, int], *, default_channel: str | None = None) -> None:
        self._supported = _normalize_supported(supported)
        if default_channel is not None and default_channel not in self._supported:
            raise ValueError(f"default channel is not supported: {default_channel!r}")
        self._default_channel = default_channel
        self._granted: dict[str, int] = {}
        self._seq: dict[str, int] = {}

    @property
    def granted(self) -> dict[str, int]:
        """Return a copy of the currently granted channels."""
        return dict(self._granted)

    def is_negotiated(self, channel: str | None = None) -> bool:
        """Return whether *channel* is negotiated, defaulting to the configured primary channel."""
        selected = self._select_channel(channel)
        return selected in self._granted

    def handle_hello(
        self,
        hello: ChannelHello | Mapping[str, Any],
        *,
        ack_fields: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Negotiate channel versions and return a ``hello_ack`` payload."""
        extra = dict(ack_fields or {})
        reserved = {"type", "channels"} & extra.keys()
        if reserved:
            raise ValueError(f"reserved hello_ack field: {sorted(reserved)[0]}")
        requested = hello.channels if isinstance(hello, ChannelHello) else _coerce_channel_map(hello.get("channels"))
        self._granted = _negotiate(self._supported, requested)
        return {"type": "hello_ack", "channels": dict(self._granted), **extra}

    def next_seq(self, channel: str | None = None) -> int:
        """Increment and return the sequence number for *channel*."""
        selected = self._select_channel(channel)
        self._seq[selected] = self._seq.get(selected, 0) + 1
        return self._seq[selected]

    def export_grants(self) -> dict[str, int]:
        """Return a serializable granted-channel map."""
        return dict(self._granted)

    def restore_grants(self, grants: Mapping[str, int]) -> None:
        """Restore persisted grants and reset sequence counters for a fresh channel instance."""
        self._granted = _negotiate(self._supported, _coerce_channel_map(grants))
        self._seq = {}

    def _select_channel(self, channel: str | None) -> str:
        selected = channel if channel is not None else self._default_channel
        if selected is None:
            raise ValueError("channel is required when no default_channel is configured")
        return selected


def parse_channel_hello(raw: str) -> ChannelHello | None:
    """Parse a framed ``hello`` payload, or return ``None`` when it is not a channel hello."""
    if not raw or not is_control_frame(raw):
        return None
    decoder = ControlFrameDecoder()
    try:
        chunks = decoder.feed(raw)
        chunks += decoder.finish()
    except Exception:
        return None
    for chunk in chunks:
        if isinstance(chunk, ControlChunk) and chunk.control.get("type") == "hello":
            try:
                return ChannelHello(channels=_coerce_channel_map(chunk.control.get("channels")))
            except ValueError:
                return None
    return None


def _normalize_supported(supported: Mapping[str, int]) -> dict[str, int]:
    normalized = _coerce_channel_map(supported)
    if not normalized:
        raise ValueError("at least one supported channel is required")
    return normalized


def _coerce_channel_map(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError("channels must be a mapping")
    channels: dict[str, int] = {}
    for name, version in value.items():
        if not isinstance(name, str) or not name:
            raise ValueError("channel names must be non-empty strings")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("channel versions must be integers")
        channels[name] = version
    return channels


def _negotiate(supported: Mapping[str, int], requested: Mapping[str, int]) -> dict[str, int]:
    granted: dict[str, int] = {}
    for name, version in requested.items():
        supported_version = supported.get(name)
        if supported_version is not None and version > 0:
            granted[name] = min(version, supported_version)
    return granted
