#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript ``channels`` port.

Channel negotiation decides which typed channels a connection may use and at
which version, so both the grant arithmetic and the rejection rules are wire
behaviour.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_channels_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.channels import NegotiatedChannels, parse_channel_hello

from provide.uterm.control_channel import encode_control_frame

OUT = Path(__file__).with_name("channels_golden.json")

# (name, supported, requested) — the grant is min(requested, supported) for
# every channel both sides know, dropping anything requested at version <= 0.
NEGOTIATE_CASES: list[tuple[str, dict[str, int], dict[str, int]]] = [
    ("exact match", {"term": 1}, {"term": 1}),
    ("client asks for less", {"term": 3}, {"term": 2}),
    ("client asks for more", {"term": 2}, {"term": 5}),
    ("unknown channel is dropped", {"term": 1}, {"other": 1}),
    ("mixed known and unknown", {"term": 1, "gui": 2}, {"term": 1, "other": 9, "gui": 5}),
    ("version zero is refused", {"term": 1}, {"term": 0}),
    ("negative version is refused", {"term": 1}, {"term": -1}),
    ("empty request grants nothing", {"term": 1}, {}),
    ("server supports more than asked", {"term": 1, "gui": 1}, {"term": 1}),
    ("multiple channels all granted", {"a": 4, "b": 5, "c": 6}, {"a": 9, "b": 2, "c": 6}),
]

# Payloads rejected by the channel-map coercion.
INVALID_CHANNEL_MAPS: list[Any] = [
    None,
    [],
    "term",
    1,
    {"": 1},
    {"term": "1"},
    {"term": 1.5},
    {"term": None},
    # A bool is an int in Python but is refused explicitly.
    {"term": True},
    {"term": False},
]

# Framed messages handed to parse_channel_hello.
PARSE_CASES: list[tuple[str, str]] = [
    ("empty string", ""),
    ("plain text", "not a frame"),
    ("hello with channels", encode_control_frame({"type": "hello", "channels": {"term": 2}})),
    ("hello with several channels", encode_control_frame({"type": "hello", "channels": {"a": 1, "b": 2}})),
    ("hello with no channels field", encode_control_frame({"type": "hello"})),
    ("hello with an empty channel map", encode_control_frame({"type": "hello", "channels": {}})),
    ("hello with an invalid channel map", encode_control_frame({"type": "hello", "channels": {"term": "x"}})),
    ("hello with a non-mapping channels field", encode_control_frame({"type": "hello", "channels": [1]})),
    ("a frame that is not a hello", encode_control_frame({"type": "hello_ack", "channels": {"term": 1}})),
    ("a frame with no type", encode_control_frame({"channels": {"term": 1}})),
    ("a structurally valid frame whose payload is not JSON", "\x10\x0200000003:abc"),
    ("a truncated frame", encode_control_frame({"type": "hello"})[:-1]),
]


def _negotiate_record(name: str, supported: dict[str, int], requested: dict[str, int]) -> dict[str, Any]:
    """Run one negotiation and record the ack payload and grant map."""
    channels = NegotiatedChannels(supported, default_channel=next(iter(supported)))
    ack = channels.handle_hello({"type": "hello", "channels": requested})
    return {
        "name": name,
        "supported": supported,
        "requested": requested,
        "ack": ack,
        "granted": channels.granted,
        "exported": channels.export_grants(),
    }


def _sequence_record() -> dict[str, Any]:
    """Sequence counters are per channel and start at one."""
    channels = NegotiatedChannels({"term": 1, "gui": 1}, default_channel="term")
    channels.handle_hello({"type": "hello", "channels": {"term": 1, "gui": 1}})
    steps = [
        {"channel": None, "seq": channels.next_seq()},
        {"channel": None, "seq": channels.next_seq()},
        {"channel": "gui", "seq": channels.next_seq("gui")},
        {"channel": "term", "seq": channels.next_seq("term")},
        {"channel": "gui", "seq": channels.next_seq("gui")},
        # A channel that was never granted still gets its own counter.
        {"channel": "other", "seq": channels.next_seq("other")},
    ]
    restored = NegotiatedChannels({"term": 1, "gui": 1}, default_channel="term")
    restored.restore_grants(channels.export_grants())
    steps_after_restore = [
        {"channel": "term", "seq": restored.next_seq("term")},
        {"channel": "gui", "seq": restored.next_seq("gui")},
    ]
    return {
        "steps": steps,
        "restored_granted": restored.granted,
        "steps_after_restore": steps_after_restore,
    }


def _ack_fields_records() -> list[dict[str, Any]]:
    """Extra ack fields merge in; the two reserved names are refused."""
    records: list[dict[str, Any]] = []
    for extra in ({"session_id": "s1"}, {"a": 1, "b": [2]}, {}):
        channels = NegotiatedChannels({"term": 1}, default_channel="term")
        ack = channels.handle_hello({"type": "hello", "channels": {"term": 1}}, ack_fields=extra)
        records.append({"extra": extra, "ack": ack, "error": None})
    for extra in ({"type": "x"}, {"channels": {}}, {"type": "x", "channels": {}}):
        channels = NegotiatedChannels({"term": 1}, default_channel="term")
        try:
            channels.handle_hello({"type": "hello", "channels": {"term": 1}}, ack_fields=extra)
        except ValueError as exc:
            records.append({"extra": extra, "ack": None, "error": str(exc)})
    return records


def _error_records() -> list[dict[str, Any]]:
    """Constructor and selection errors, with their exact messages."""
    records: list[dict[str, Any]] = []

    def record(name: str, fn: Any) -> None:
        try:
            fn()
        except ValueError as exc:
            records.append({"name": name, "error": str(exc)})
        else:
            records.append({"name": name, "error": None})

    record("empty supported map", lambda: NegotiatedChannels({}))
    record("default channel not supported", lambda: NegotiatedChannels({"term": 1}, default_channel="gui"))
    record("next_seq with no default", lambda: NegotiatedChannels({"term": 1}).next_seq())
    record("is_negotiated with no default", lambda: NegotiatedChannels({"term": 1}).is_negotiated())
    for index, value in enumerate(INVALID_CHANNEL_MAPS):
        record(f"invalid supported map {index}", lambda v=value: NegotiatedChannels(v))
    return records


def _is_negotiated_records() -> list[dict[str, Any]]:
    """is_negotiated resolves the default channel when none is named."""
    channels = NegotiatedChannels({"term": 1, "gui": 1}, default_channel="term")
    before = {
        "default": channels.is_negotiated(),
        "term": channels.is_negotiated("term"),
        "gui": channels.is_negotiated("gui"),
    }
    channels.handle_hello({"type": "hello", "channels": {"term": 1}})
    after = {
        "default": channels.is_negotiated(),
        "term": channels.is_negotiated("term"),
        "gui": channels.is_negotiated("gui"),
        "unknown": channels.is_negotiated("nope"),
    }
    return [{"stage": "before", **before}, {"stage": "after", **after}]


def main() -> int:
    """Write the golden corpus and report the record count."""
    parse_records = []
    for name, raw in PARSE_CASES:
        hello = parse_channel_hello(raw)
        parse_records.append({"name": name, "raw": raw, "channels": None if hello is None else hello.channels})

    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_channels_golden.py",
        "negotiate": [_negotiate_record(*case) for case in NEGOTIATE_CASES],
        "parse": parse_records,
        "sequence": _sequence_record(),
        "ack_fields": _ack_fields_records(),
        "errors": _error_records(),
        "is_negotiated": _is_negotiated_records(),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    total = len(payload["negotiate"]) + len(parse_records) + len(payload["ack_fields"]) + len(payload["errors"])
    print(f"wrote {OUT} ({total} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
