#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Replay the cross-language differential fuzz corpus against the CPython reference.

The corpus (``conformance/fuzz/control_channel_fuzz.json``) is the contract three
other ports assert against. This suite is the reference side of it, and it does
two jobs:

1. **The corpus is self-consistent and replayable.** Ids are unique, counts
   match, every ``*_b64`` field decodes, the whole document is ASCII, and the
   recorded rejections actually cover every error the codec can raise.
2. **The reference still agrees with what was recorded.** If ``control_channel``
   changes behaviour and the corpus is not regenerated, this goes red — which is
   the same red the Go, C# and TypeScript ports would go, only sooner.

Deliberately written as an *independent* replayer: it does not import the
generator. Anything it needs to know about the format it reads out of the file,
exactly the way a port with no Python has to. If this suite can replay the
corpus, the format is replayable.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from provide.uterm.control_channel import (
    ControlChunk,
    ControlFrameDecoder,
    ControlFrameProtocolError,
    DataChunk,
    encode_control_frame,
    encode_terminal_data,
    is_control_frame,
)

SCHEMA = "provide-uterm/control-channel-fuzz/1"
CORPUS_PATH = Path(__file__).resolve().parents[4] / "conformance" / "fuzz" / "control_channel_fuzz.json"
CORPUS_TEXT = CORPUS_PATH.read_text(encoding="utf-8")
CORPUS: dict[str, Any] = json.loads(CORPUS_TEXT)

FAMILIES = (
    "encode_data",
    "encode_control",
    "is_control_frame",
    "decode",
    "regressions",
    "serializer_divergences",
)
ID_PREFIXES = {
    "encode_data": "CCF-ED-",
    "encode_control": "CCF-EC-",
    "is_control_frame": "CCF-PR-",
    "decode": "CCF-DC-",
    "regressions": "CCF-REG-",
    "serializer_divergences": "CCF-SD-",
}
# Every message ``ControlFrameDecoder`` can raise. The corpus is only doing its
# job if it reaches all of them; a generator change that silently stopped
# producing, say, oversized headers would otherwise go unnoticed.
EXPECTED_ERRORS = frozenset(
    {
        "invalid control prefix",
        "invalid control header",
        "invalid control json",
        "invalid control payload length",
        "control payload must be an object",
        "control payload too large",
        "control payload nests deeper than 32",
        "truncated control frame",
    }
)


def _decode_b64(value: str) -> str:
    """Recover a codec string from a ``*_b64`` field."""
    return base64.b64decode(value.encode("ascii"), validate=True).decode("utf-8")


def _event(event: DataChunk | ControlChunk) -> dict[str, Any]:
    if isinstance(event, DataChunk):
        return {"kind": "data", "data_b64": base64.b64encode(event.data.encode("utf-8")).decode("ascii")}
    return {"kind": "control", "control": event.control}


def _drive(chunks: list[str], *, finish: bool) -> dict[str, Any]:
    """Replay one drive exactly as README.md tells a port to."""
    on_error: list[str] = []
    decoder = ControlFrameDecoder(on_error=on_error.append)
    events: list[dict[str, Any]] = []
    error: str | None = None
    try:
        for chunk in chunks:
            events.extend(_event(item) for item in decoder.feed(chunk))
        if finish:
            events.extend(_event(item) for item in decoder.finish())
    except ControlFrameProtocolError as exc:
        error = str(exc)
    return {"events": events, "error": error, "on_error": on_error}


def _decode_cases() -> list[dict[str, Any]]:
    """The decode family plus the regression family — identical shape."""
    return [*CORPUS["decode"], *CORPUS["regressions"]]


class TestCorpusIntegrity:
    """The corpus itself is well-formed, before anything is replayed through it."""

    def test_schema_and_seed_are_declared(self) -> None:
        assert CORPUS["schema"] == SCHEMA
        assert isinstance(CORPUS["seed"], int)
        assert CORPUS["generator"] == "conformance/fuzz/gen_control_channel_fuzz.py"

    def test_document_is_pure_ascii(self) -> None:
        """No reader has to agree with CPython about file encoding."""
        assert CORPUS_TEXT.isascii()

    def test_declared_counts_match_the_families(self) -> None:
        assert set(CORPUS["counts"]) == set(FAMILIES)
        for family in FAMILIES:
            assert CORPUS["counts"][family] == len(CORPUS[family]), family

    def test_case_ids_are_unique_and_correctly_prefixed(self) -> None:
        seen: set[str] = set()
        for family in FAMILIES:
            for case in CORPUS[family]:
                case_id = case["id"]
                assert case_id.startswith(ID_PREFIXES[family]), f"{case_id} is not in {family}"
                assert case_id not in seen, f"duplicate case id {case_id}"
                seen.add(case_id)
        assert len(seen) == sum(CORPUS["counts"].values())

    def test_every_b64_field_decodes(self) -> None:
        for family in FAMILIES:
            for case in CORPUS[family]:
                for key, value in case.items():
                    if not key.endswith("_b64"):
                        continue
                    for item in value if isinstance(value, list) else [value]:
                        _decode_b64(item)
                for record in ("chunked", "single"):
                    for event in case.get(record, {}).get("events", []):
                        if event["kind"] == "data":
                            _decode_b64(event["data_b64"])

    def test_no_floats_in_the_asserted_families(self) -> None:
        """Rule 3: a float would break structural comparison in the other ports.

        ``serializer_divergences`` is exempt — pinning float rendering is the
        entire reason that family exists, and no port asserts equality on it.
        """
        stack: list[Any] = [{name: CORPUS[name] for name in FAMILIES if name != "serializer_divergences"}]
        while stack:
            node = stack.pop()
            assert not isinstance(node, float), f"corpus carries a float: {node!r}"
            if isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)

    def test_the_corpus_reaches_every_rejection_path(self) -> None:
        observed = {
            case[drive]["error"] for case in _decode_cases() for drive in ("chunked", "single") if case[drive]["error"]
        }
        assert observed == EXPECTED_ERRORS

    def test_the_corpus_contains_streams_that_decode_cleanly(self) -> None:
        """A corpus that only ever rejects would never test the buffering."""
        clean = [case for case in CORPUS["decode"] if case["chunked"]["error"] is None]
        assert len(clean) > 20
        assert any(event["kind"] == "control" for case in clean for event in case["chunked"]["events"])

    def test_chunked_and_single_drives_genuinely_diverge_somewhere(self) -> None:
        """The whole reason both drives are recorded."""
        divergent = [case for case in _decode_cases() if case["chunked"] != case["single"]]
        assert len(divergent) > 10

    def test_on_error_fires_at_most_once_per_drive(self) -> None:
        for case in _decode_cases():
            for drive in ("chunked", "single"):
                record = case[drive]
                expected = ["control_frame_protocol_error"] if record["error"] else []
                assert record["on_error"] == expected, f"{case['id']}/{drive}"

    def test_regressions_carry_a_note(self) -> None:
        assert CORPUS["regressions"], "at least one permanent regression case must exist"
        for case in CORPUS["regressions"]:
            assert case["note"].strip()


class TestReferenceStillAgrees:
    """Replay every recorded case against the live reference implementation."""

    def test_encode_data(self) -> None:
        for case in CORPUS["encode_data"]:
            data = _decode_b64(case["in_b64"])
            assert encode_terminal_data(data) == _decode_b64(case["out_b64"]), case["id"]

    def test_encode_control(self) -> None:
        for case in CORPUS["encode_control"]:
            assert encode_control_frame(case["payload"]) == _decode_b64(case["out_b64"]), case["id"]

    def test_encode_control_keys_are_in_ascending_order(self) -> None:
        """Go sorts map keys; the others preserve insertion order. Both must agree."""
        for case in CORPUS["encode_control"]:
            keys = list(case["payload"])
            assert keys == sorted(keys), case["id"]

    def test_is_control_frame(self) -> None:
        for case in CORPUS["is_control_frame"]:
            assert is_control_frame(_decode_b64(case["in_b64"])) is case["out"], case["id"]

    @pytest.mark.parametrize("drive", ["chunked", "single"])
    def test_decode(self, drive: str) -> None:
        for case in _decode_cases():
            chunks = [_decode_b64(chunk) for chunk in case["chunks_b64"]]
            if drive == "single":
                chunks = ["".join(chunks)]
            actual = _drive(chunks, finish=case["finish"])
            assert actual == case[drive], f"{case['id']} ({drive})"

    def test_serializer_divergences(self) -> None:
        """Recorded, never asserted equal across ports — but CPython must not move."""
        for case in CORPUS["serializer_divergences"]:
            assert encode_control_frame(case["payload"]) == _decode_b64(case["cpython_out_b64"]), case["id"]
