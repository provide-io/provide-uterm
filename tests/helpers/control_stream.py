#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Test helpers for the inline control stream protocol."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from provide.terminal.client.control_ws import LogicalFrameDecoder, encode_logical_frame


def encode_frame(payload: Mapping[str, Any]) -> str:
    """Encode one logical WS frame using the inline control stream."""
    return encode_logical_frame(payload)


def decode_chunk(raw: str, *, data_type: str) -> list[dict[str, Any]]:
    """Decode one WS chunk into logical test frames."""
    role = "worker" if data_type == "input" else "browser"
    decoder = LogicalFrameDecoder(role=role)
    return decoder.feed(raw) + decoder.finish()


class IncrementalFrameDecoder:
    """Incremental decoder wrapper that yields logical test frames."""

    def __init__(self, *, data_type: str) -> None:
        role = "worker" if data_type == "input" else "browser"
        self._decoder = LogicalFrameDecoder(role=role)

    def feed(self, raw: str) -> list[dict[str, Any]]:
        return self._decoder.feed(raw)

    def finish(self) -> list[dict[str, Any]]:
        return self._decoder.finish()
