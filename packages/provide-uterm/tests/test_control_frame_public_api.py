#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Public DLE/STX control-frame API tests."""

from __future__ import annotations

import json
import re
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from provide.uterm.control_channel import (
    ControlChunk,
    ControlFrameDecoder,
    DataChunk,
    encode_control_frame,
    encode_terminal_data,
)


def _split_everywhere(text: str) -> list[str]:
    return [text[i : i + 1] for i in range(len(text))]


def _coalesce_data(events: list[ControlChunk | DataChunk]) -> list[ControlChunk | DataChunk]:
    logical: list[ControlChunk | DataChunk] = []
    data_parts: list[str] = []
    for event in events:
        if isinstance(event, DataChunk):
            data_parts.append(event.data)
            continue
        if data_parts:
            logical.append(DataChunk("".join(data_parts)))
            data_parts.clear()
        logical.append(event)
    if data_parts:
        logical.append(DataChunk("".join(data_parts)))
    return logical


def test_public_control_frame_api_round_trips_mixed_stream_one_byte_at_a_time() -> None:
    payload = {"type": "hello", "text": "utf8 👋", "nested": {"ok": True}}
    stream = "pre" + encode_control_frame(payload) + encode_terminal_data("\x10post")
    decoder = ControlFrameDecoder()
    events = []

    for part in _split_everywhere(stream):
        events.extend(decoder.feed(part))
    events.extend(decoder.finish())

    assert _coalesce_data(events) == [
        DataChunk("pre"),
        ControlChunk(payload),
        DataChunk("\x10post"),
    ]


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=8),
        st.one_of(st.none(), st.booleans(), st.integers(-100, 100), st.text(max_size=16)),
        min_size=1,
        max_size=6,
    )
)
def test_control_frame_api_uses_utf8_byte_lengths(payload: dict[str, object]) -> None:
    payload.setdefault("type", "property")
    encoded = encode_control_frame(payload)
    length_hex = encoded[2:10]
    raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    assert int(length_hex, 16) == len(raw_json.encode("utf-8"))
    decoder = ControlFrameDecoder()
    assert decoder.feed(encoded) == [ControlChunk(payload)]


def test_control_frame_source_has_no_legacy_control_channel_api_names() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    banned = (
        "ControlChannelProtocolError",
        "ControlChannelChunk",
        "ControlChannelDecoder",
        "encode_control",
        "encode_data",
        "is_control_framed",
    )
    offenders: list[str] = []
    for path in source_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in banned:
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", text):
                offenders.append(f"{path.relative_to(source_root)}:{token}")

    assert offenders == []


def test_source_does_not_send_bare_json_over_control_websockets() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    source_roots = (
        repo_root / "packages/provide-uterm/src",
        repo_root / "packages/provide-uterm-client/src",
        repo_root / "packages/provide-uterm-server/src",
        repo_root / "packages/provide-uterm-cloudflare/src",
    )
    bare_json_send = re.compile(r"\.send(?:_text)?\(\s*json\.dumps\(")
    offenders: list[str] = []
    for source_root in source_roots:
        for path in source_root.rglob("*.py"):
            if bare_json_send.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(repo_root)))

    assert offenders == []
