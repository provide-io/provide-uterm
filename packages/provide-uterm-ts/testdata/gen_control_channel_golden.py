#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript ``controlChannel`` port.

This is the wire format itself, so the corpus is deliberately hostile: it
covers the encoder, the structural ``is_control_frame`` predicate, and the
incremental decoder driven chunk-by-chunk, including every rejection path.

Strings are carried as JSON, so a code point that JSON cannot round-trip
would corrupt the corpus rather than the port. Every payload here stays
within the BMP and avoids lone surrogates.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_control_channel_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.control_channel import (
    ControlChunk,
    ControlFrameDecoder,
    ControlFrameProtocolError,
    DataChunk,
    encode_control_frame,
    encode_terminal_data,
    is_control_frame,
)

OUT = Path(__file__).with_name("control_channel_golden.json")

DLE = "\x10"
STX = "\x02"


def _frame(payload: dict[str, Any]) -> str:
    """Shorthand for a well-formed control frame."""
    return encode_control_frame(payload)


ENCODE_DATA_INPUTS: list[str] = [
    "",
    "plain terminal output",
    DLE,
    DLE + DLE,
    "before" + DLE + "after",
    DLE + "leading",
    "trailing" + DLE,
    DLE + STX + "not a frame, just bytes",
    # High latin-1 bytes are what the ws_bytes shim carries.
    "\xff\xfe\x80\x9f",
    "box \xc9\xcd\xbb drawing",
    "\n\r\t",
]

ENCODE_CONTROL_INPUTS: list[dict[str, Any]] = [
    {},
    {"type": "hello"},
    {"type": "hello", "version": 1},
    # Key order is insertion order on both sides for non-numeric keys.
    {"b": 1, "a": 2},
    {"type": "resize", "cols": 80, "rows": 24},
    {"nested": {"a": [1, 2, {"b": None}]}},
    {"flag_true": True, "flag_false": False, "nothing": None},
    {"float": 1.5, "negative": -2.25},
    {"big_int": 2**53 - 1},
    {"empty_list": [], "empty_obj": {}},
    # Non-ASCII payloads: the header counts UTF-8 bytes, not characters.
    {"text": "é"},
    {"text": "你好"},
    {"text": "aéb你c"},
    {"emoji": "☃"},
    # An astral code point is four UTF-8 bytes and two UTF-16 units, which is
    # where a character-indexed and a unit-indexed payload walk can diverge.
    {"astral": "𝄞"},
    {"astral_pair": "a𝄞b"},
    # Characters JSON must escape.
    {"quote": '"'},
    {"backslash": "\\"},
    {"newline": "\n", "tab": "\t"},
    {"control": "\x00\x1f"},
    # A payload containing the framing bytes themselves.
    {"dle": DLE, "stx": STX},
    # Deep but legal nesting (the decoder rejects deeper than 32).
    {"d": {"d": {"d": {"d": {"d": 1}}}}},
]


# Payloads whose JSON rendering genuinely differs between CPython and the
# host runtime. Python is the only one of the four implementations that keeps
# an int/float distinction through JSON: it writes ``0.0`` where Go's
# encoding/json, .NET's System.Text.Json and ECMAScript's JSON.stringify all
# write ``0``. The general control-frame encoder tolerates this, exactly as
# the Go and C# ports do; the canonical-JSON path used for HMAC identity
# signatures does not, and reproduces CPython's float repr explicitly.
FLOAT_DIVERGENCE_INPUTS: list[dict[str, Any]] = [
    {"zero": 0.0},
    {"one": 1.0},
    {"negative_zero": -0.0},
    {"mixed": [1.0, 1.5, 2]},
]


def _is_control_frame_inputs() -> list[str]:
    """Structural predicate corpus, valid and invalid."""
    good = _frame({"type": "hello"})
    return [
        "",
        "x",
        good,
        # Too short to hold a header.
        good[:10],
        good[:11],
        # Truncated payload.
        good[:-1],
        # Trailing bytes after a complete frame.
        good + "x",
        # Wrong magic.
        "\x11\x02" + good[2:],
        DLE + "\x03" + good[2:],
        # Wrong separator.
        good[:10] + ";" + good[11:],
        # Non-hex length.
        DLE + STX + "0000000g" + ":" + "{}",
        DLE + STX + "        " + ":" + "{}",
        # Upper-case hex is valid hex but is not the canonical %08x form.
        DLE + STX + "0000000A" + ":" + '{"a":"aaaaaaaaa"}',
        # Length larger than the 1 MiB ceiling.
        DLE + STX + "00100001" + ":" + "{}",
        # Declared length longer than what is present.
        DLE + STX + "000000ff" + ":" + "{}",
        # Declared length that splits a multi-byte code point.
        DLE + STX + "00000001" + ":" + "é",
        # A frame whose payload is not JSON is still structurally a frame.
        DLE + STX + "00000003" + ":" + "abc",
        # Empty payload.
        DLE + STX + "00000000" + ":",
        # Non-ASCII payload where bytes and characters differ.
        _frame({"t": "你好"}),
    ]


# Each case is a list of chunks fed to a fresh decoder, then finish().
DECODE_CASES: list[tuple[str, list[str]]] = [
    ("empty stream", []),
    ("empty chunk", [""]),
    ("plain data", ["hello world"]),
    ("plain data split across chunks", ["hel", "lo ", "world"]),
    ("escaped DLE alone", [DLE + DLE]),
    ("escaped DLE between data", ["a" + DLE + DLE + "b"]),
    ("escaped DLE split across chunks", ["a" + DLE, DLE + "b"]),
    ("single control frame", [_frame({"type": "hello"})]),
    ("control frame split at every byte", None),  # filled in below
    ("data then control", ["out" + _frame({"type": "hello"})]),
    ("control then data", [_frame({"type": "hello"}) + "out"]),
    ("data control data", ["a" + _frame({"type": "x"}) + "b"]),
    ("two adjacent control frames", [_frame({"a": 1}) + _frame({"b": 2})]),
    ("control frame with a non-ASCII payload", [_frame({"t": "你好"})]),
    # The depth walker has to skip primitives and nulls inside arrays as well
    # as inside objects, or a null child would be walked as a container.
    ("control frame with nulls and objects inside an array", [_frame({"list": [None, 1, {"x": 2}, [None]]})]),
    ("escaped DLE inside data around a frame", ["a" + DLE + DLE + _frame({"a": 1}) + DLE + DLE]),
    ("trailing lone DLE is buffered, not emitted", ["data" + DLE]),
    ("header split across chunks", None),  # filled in below
    ("payload split across chunks", None),  # filled in below
    ("multi-byte code point split across chunks", None),  # filled in below
    ("astral code point split across chunks", None),  # filled in below
]


def _expand_split_cases() -> list[tuple[str, list[str]]]:
    """Build the cases that need a computed chunk split."""
    frame = _frame({"type": "hello", "n": 1})
    unicode_frame = _frame({"t": "你好"})
    cases: list[tuple[str, list[str]]] = []
    for name, chunks in DECODE_CASES:
        if chunks is not None:
            cases.append((name, chunks))
            continue
        if name == "control frame split at every byte":
            cases.append((name, list(frame)))
        elif name == "header split across chunks":
            cases.append((name, [frame[:5], frame[5:]]))
        elif name == "payload split across chunks":
            cases.append((name, [frame[:13], frame[13:]]))
        elif name == "multi-byte code point split across chunks":
            split = unicode_frame.index("你")
            cases.append((name, [unicode_frame[:split], unicode_frame[split:]]))
        else:  # astral code point split across chunks
            astral_frame = _frame({"t": "a𝄞b"})
            split = astral_frame.index("𝄞")
            cases.append((name, [astral_frame[:split], astral_frame[split:]]))
    return cases


# Streams that must be rejected, and the stage that rejects them.
REJECT_CASES: list[tuple[str, list[str], bool]] = [
    # (name, chunks, finish) — `finish` says whether finish() is also called.
    ("DLE followed by a byte that is neither DLE nor STX", ["a" + DLE + "x"], False),
    ("invalid control header separator", [DLE + STX + "00000002" + ";" + "{}"], False),
    ("non-hex control header", [DLE + STX + "0000000g" + ":" + "{}"], False),
    ("control payload over the ceiling", [DLE + STX + "00100001" + ":"], False),
    ("payload that is not JSON", [DLE + STX + "00000003" + ":" + "abc"], False),
    ("payload that is a JSON array", [DLE + STX + "00000002" + ":" + "[]"], False),
    ("payload that is a JSON string", [DLE + STX + "00000003" + ":" + '"a"'], False),
    ("payload that is a JSON number", [DLE + STX + "00000001" + ":" + "1"], False),
    ("declared length splitting a code point", [DLE + STX + "00000001" + ":" + "é"], False),
    ("truncated header at finish", [DLE + STX + "0000"], True),
    ("truncated payload at finish", [DLE + STX + "00000010" + ":" + "{}"], True),
    ("lone trailing DLE at finish", ["data" + DLE], True),
    ("nesting deeper than the depth limit", None, False),
]


def _deep_payload(depth: int) -> str:
    """A control frame whose JSON nests `depth` levels."""
    body = "1"
    for _ in range(depth):
        body = "[" + body + "]"
    serialized = '{"d":' + body + "}"
    return f"{DLE}{STX}{len(serialized.encode('utf-8')):08x}:{serialized}"


def _chunk_events(chunks: list[str], *, finish: bool) -> dict[str, Any]:
    """Drive a fresh decoder and record what it emitted or raised."""
    errors: list[str] = []
    decoder = ControlFrameDecoder(on_error=errors.append)
    emitted: list[dict[str, Any]] = []
    try:
        for chunk in chunks:
            for event in decoder.feed(chunk):
                emitted.append(_event_to_json(event))
        if finish:
            for event in decoder.finish():
                emitted.append(_event_to_json(event))
    except ControlFrameProtocolError as exc:
        return {"events": emitted, "error": str(exc), "on_error": errors}
    return {"events": emitted, "error": None, "on_error": errors}


def _event_to_json(event: DataChunk | ControlChunk) -> dict[str, Any]:
    """Render a decoded event as a JSON-safe record."""
    if isinstance(event, DataChunk):
        return {"kind": event.kind, "data": event.data}
    return {"kind": event.kind, "control": event.control}


def main() -> int:
    """Write the golden corpus and report the record count."""
    encode_data = [{"data": value, "out": encode_terminal_data(value)} for value in ENCODE_DATA_INPUTS]
    encode_control = [{"payload": payload, "out": encode_control_frame(payload)} for payload in ENCODE_CONTROL_INPUTS]
    predicate = [{"message": value, "out": is_control_frame(value)} for value in _is_control_frame_inputs()]

    decode = []
    for name, chunks in _expand_split_cases():
        decode.append({"name": name, "chunks": chunks, **_chunk_events(chunks, finish=True)})

    reject = []
    for name, chunks, finish in REJECT_CASES:
        actual_chunks = [_deep_payload(40)] if chunks is None else chunks
        reject.append(
            {"name": name, "chunks": actual_chunks, "finish": finish, **_chunk_events(actual_chunks, finish=finish)}
        )

    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_control_channel_golden.py",
        "encode_data": encode_data,
        "encode_control": encode_control,
        "float_divergences": [{"payload": p, "cpython": encode_control_frame(p)} for p in FLOAT_DIVERGENCE_INPUTS],
        "is_control_frame": predicate,
        "decode": decode,
        "reject": reject,
    }
    # sort_keys is deliberately off: the encoder preserves insertion order,
    # so sorting would rewrite the recorded payloads and hide that contract.
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    total = len(encode_data) + len(encode_control) + len(predicate) + len(decode) + len(reject)
    print(f"wrote {OUT} ({total} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
