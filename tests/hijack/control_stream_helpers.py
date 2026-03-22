# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Any

from provide.terminal.control_stream import ControlChunk, ControlStreamDecoder


def decode_control_payload(payload: str) -> dict[str, Any]:
    decoder = ControlStreamDecoder()
    events = decoder.feed(payload)
    events.extend(decoder.finish())
    controls = [event.control for event in events if isinstance(event, ControlChunk)]
    assert len(controls) == 1, f"expected exactly one control frame, got {len(controls)} from {payload!r}"
    return controls[0]


def decode_control_payloads(payloads: list[str]) -> list[dict[str, Any]]:
    return [decode_control_payload(payload) for payload in payloads]
