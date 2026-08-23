#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the cross-language differential fuzz corpus for the control-frame codec.

The corpus is deterministic from an integer seed: ``random.Random(seed)`` and
nothing else, so ``--seed N`` twice produces a byte-identical file. That is the
whole basis of the contract — every port replays the *same* inputs, and CI can
prove the committed file still matches the reference by regenerating it.

The format is specified in ``conformance/fuzz/README.md``. Read that before
changing anything here; three ports assert against it.

Encoding, in one paragraph, because this is where a corpus like this rots:
every input and every emitted terminal-data string is carried as **base64 of
its UTF-8 bytes** in a ``*_b64`` field, never as a JSON string. JSON cannot
carry a lone surrogate, ``json.dumps`` will happily write ``Infinity``/``NaN``
that ``JSON.parse`` rejects, and four runtimes do not agree on whether
``U+2028`` inside a JSON string literal is legal. base64 is bytes, and bytes
are the same in every language. The whole document is ASCII (asserted at write
time), so no reader has to agree about anything but base64.

Usage (from the repository root)::

    uv run python conformance/fuzz/gen_control_channel_fuzz.py
    uv run python conformance/fuzz/gen_control_channel_fuzz.py --seed 7 --out /tmp/x.json
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import sys
from pathlib import Path
from typing import Any, Final

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "packages" / "provide-uterm" / "src"))

from provide.uterm.control_channel import (  # noqa: E402
    _MAX_CONTROL_FRAME_DEPTH,
    _MAX_CONTROL_PAYLOAD_BYTES,
    ControlChunk,
    ControlFrameDecoder,
    ControlFrameProtocolError,
    DataChunk,
    encode_control_frame,
    encode_terminal_data,
    is_control_frame,
)

SCHEMA = "provide-uterm/control-channel-fuzz/1"
#: The committed corpus is generated from this seed. CI regenerates with it and
#: fails on any difference, so every port is held to identical inputs.
CORPUS_SEED = 20260729
OUT = Path(__file__).with_name("control_channel_fuzz.json")

DLE = "\x10"
STX = "\x02"
HEADER_BYTES = 11

# How many cases each fuzz family contributes. Weighted toward the incremental
# decoder: it is the only *stateful* surface, so it is the only one where a
# port can be right about every single input and still desynchronise.
COUNTS = {"encode_data": 96, "encode_control": 96, "is_control_frame": 128, "decode": 192}

# ---------------------------------------------------------------------------
# Alphabets
# ---------------------------------------------------------------------------

# Raw-string alphabet, used everywhere the codec does NOT run the value through
# a JSON serializer (terminal data, predicate inputs, decoder streams). The
# weight is per *class*, not per character, so the 128-character latin-1 class
# cannot drown out DLE: uniformly random text over Unicode essentially never
# produces the one byte the whole format turns on. DLE lands in ~14% of draws.
_RAW_POOL: tuple[tuple[str, int], ...] = (
    (DLE, 10),
    (STX, 6),
    (":", 4),
    ("0123456789abcdefABCDEF", 8),
    ('{}[]",\\', 6),
    ("abcxyz ABCXYZ.-_/", 10),
    ("\x00\x01\x03\x07\x0b\x0e\x1b\x1f\x7f", 6),
    # Latin-1 high bytes: what the ws_bytes shim turns raw CP437 into.
    ("".join(chr(c) for c in range(0x80, 0x100)), 8),
    # Multi-byte code points: the header counts UTF-8 *bytes*, not characters.
    ("éñÿΩЖ€─│☃你好あ한", 8),
    # Astral: 4 UTF-8 bytes, 2 UTF-16 units — where a character-indexed and a
    # unit-indexed payload walk diverge.
    ("𝄞😀🜁", 6),
)
_RAW_CLASSES = [chars for chars, weight in _RAW_POOL for _ in range(weight)]

# JSON-value alphabet, used ONLY for strings that go through a port's JSON
# *serializer* (the ``encode_control`` family). Restricted to code points where
# CPython's ``json``, Go's ``encoding/json`` (SetEscapeHTML(false)), .NET's
# ``System.Text.Json`` (UnsafeRelaxedJsonEscaping) and ECMAScript's
# ``JSON.stringify`` produce byte-identical output. Verified empirically; the
# excluded classes and why are listed in README.md under "Serializer
# divergences". Everything excluded here is still fuzzed on the other three
# surfaces, which never re-serialize.
_JSON_SAFE_ALPHABET = "\b\t\n\f\r" + "".join(chr(c) for c in range(0x20, 0x7F)) + "¡¢éñÿ" + "ΩαЖה" + "€─│☃" + "あ你好한"

_KEY_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789_"


def _b64(text: str) -> str:
    """Encode *text* as base64 of its UTF-8 bytes."""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------------------
# Random input builders
# ---------------------------------------------------------------------------


def rand_raw_char(rng: random.Random) -> str:
    """One character: pick a class by weight, then a character inside it."""
    return rng.choice(rng.choice(_RAW_CLASSES))


def rand_raw_text(rng: random.Random, max_len: int) -> str:
    """A hostile string over the raw alphabet, length 0..max_len."""
    return "".join(rand_raw_char(rng) for _ in range(rng.randint(0, max_len)))


def _rand_json_text(rng: random.Random, max_len: int) -> str:
    """A string safe to run through any of the four JSON serializers."""
    return "".join(rng.choice(_JSON_SAFE_ALPHABET) for _ in range(rng.randint(0, max_len)))


def _rand_json_value(rng: random.Random, depth: int) -> Any:
    """A JSON value. Integers only — never a float; see README.md."""
    if depth <= 0:
        kind = rng.randint(0, 3)
    else:
        kind = rng.randint(0, 5)
    if kind == 0:
        return None
    if kind == 1:
        return rng.random() < 0.5
    if kind == 2:
        return rng.randint(-(2**31), 2**31 - 1)
    if kind == 3:
        return _rand_json_text(rng, 12)
    if kind == 4:
        return [_rand_json_value(rng, depth - 1) for _ in range(rng.randint(0, 3))]
    return {_rand_key(rng, i): _rand_json_value(rng, depth - 1) for i in range(rng.randint(0, 3))}


def _rand_key(rng: random.Random, index: int) -> str:
    """An object key. Prefixed with its position so insertion order == sorted order.

    Go marshals ``map[string]any`` with its keys sorted; CPython, .NET and
    ECMAScript all preserve insertion order. Keys that are already in ascending
    byte order make the two rules agree, so key ordering can never be the thing
    that fails a port.
    """
    tail = "".join(rng.choice(_KEY_ALPHABET) for _ in range(rng.randint(0, 5)))
    return f"k{index}{tail}"


def rand_payload(rng: random.Random) -> dict[str, Any]:
    """A control payload: a JSON object with ascending keys, depth <= 4."""
    return {_rand_key(rng, i): _rand_json_value(rng, 3) for i in range(rng.randint(0, 4))}


def _raw_frame(payload_text: str) -> str:
    """A frame around a *raw* JSON payload string, header computed honestly."""
    return f"{DLE}{STX}{len(payload_text.encode('utf-8')):08x}:{payload_text}"


# ---------------------------------------------------------------------------
# Stream segments — the vocabulary the decoder streams are built from
# ---------------------------------------------------------------------------


def _seg_data(rng: random.Random) -> str:
    return encode_terminal_data(rand_raw_text(rng, 16))


def _seg_valid_frame(rng: random.Random) -> str:
    return encode_control_frame(rand_payload(rng))


def _seg_framing_bytes_in_payload(rng: random.Random) -> str:
    """A frame whose payload *contains* raw DLE/STX bytes.

    The length header covers them, so a decoder that rescans the payload for
    framing bytes instead of trusting the header desynchronises here.
    """
    filler = "".join(rng.choice(f"{DLE}{STX}ab:") for _ in range(rng.randint(1, 6)))
    return _raw_frame(f'{{"k":"{filler}"}}')


def _seg_not_json(rng: random.Random) -> str:
    return _raw_frame(rng.choice(["abc", "[]", '"a"', "1", "null", "{", "{,}", "tru"]))


def _seg_empty_payload(_rng: random.Random) -> str:
    return _raw_frame("")


def _seg_bad_header(rng: random.Random) -> str:
    length_hex = "".join(rng.choice("0123456789abcdefgABCDEFG :") for _ in range(8))
    return DLE + STX + length_hex + rng.choice(":;,\x00") + "{}"


def _seg_oversize(rng: random.Random) -> str:
    over = _MAX_CONTROL_PAYLOAD_BYTES + rng.randint(1, 4096)
    return f"{DLE}{STX}{over:08x}:"


def _seg_lying_length(rng: random.Random) -> str:
    """A header that declares more bytes than the payload actually has."""
    body = '{"k":1}'
    return f"{DLE}{STX}{len(body) + rng.randint(1, 64):08x}:{body}"


#: Length headers chosen to break an implementation that parses this unsigned
#: wire value into a signed 32-bit accumulator. ``80000000`` and above set the
#: high bit, so a port accumulating into a signed int wraps *negative* — its
#: "payload too large" guard then never fires, and the negative length reaches
#: an index or a slice. That is not hypothetical: it is what the C# port did,
#: throwing ``IndexOutOfRangeException`` where the reference reports
#: ``control payload too large``.
#:
#: The corpus could not reach it before. ``_seg_oversize`` only ever produced
#: values just above the ceiling, and every high-bit header the generator
#: happened to emit came from ``_seg_bad_header``, which follows it with a
#: separator other than ``:`` — so the header was rejected as malformed before
#: its length was ever parsed. A header is only load-bearing when ``:`` follows.
_BOUNDARY_LENGTH_HEX: Final = (
    "7fffffff",  # the largest value a signed 32-bit accumulator still holds
    "80000000",  # the first that wraps it, and the smallest negative result
    "80000001",
    "fffffffe",
    "ffffffff",  # every bit set: wraps to -1
    "00100001",  # one byte past the ceiling, overflowing nothing
    "0010000f",
)


def _seg_boundary_length(rng: random.Random) -> str:
    """A load-bearing length header at a signed-overflow boundary."""
    return f"{DLE}{STX}{rng.choice(_BOUNDARY_LENGTH_HEX)}:" + '{"k":1}'


def _seg_upper_hex_length(rng: random.Random) -> str:
    """A valid frame whose length header uses upper-case hex digits.

    CPython accepts these (``string.hexdigits`` spans both cases) and so do the
    ports, but no generated case paired upper-case hex with ``:`` — so the
    agreement was assumed rather than tested. The payload is padded until its
    byte length has a hex digit above nine, or the header would be all digits
    and prove nothing.
    """
    body = '{"k":"' + "a" * rng.randint(20, 24) + '"}'
    return f"{DLE}{STX}{len(body.encode()):08X}:{body}"


def _seg_split_code_point(_rng: random.Random) -> str:
    """A declared length that lands inside a multi-byte code point."""
    return f"{DLE}{STX}{1:08x}:é"


def _seg_deep(_rng: random.Random) -> str:
    body = "1"
    for _ in range(_MAX_CONTROL_FRAME_DEPTH + 8):
        body = "[" + body + "]"
    return _raw_frame('{"d":' + body + "}")


def _seg_escaped_dle(rng: random.Random) -> str:
    return (DLE + DLE) * rng.randint(1, 3)


def _seg_lone_dle(rng: random.Random) -> str:
    return DLE + rng.choice("ax:\x00\x01é")


def _seg_truncated_frame(rng: random.Random) -> str:
    frame = encode_control_frame(rand_payload(rng))
    return frame[: rng.randint(1, max(1, len(frame) - 1))]


# Weights are chosen so roughly a third of streams are entirely well-formed. A
# corpus where almost every stream dies on its first segment would only ever
# test the rejection paths, and the decoder's buffering is what desynchronises.
_SEGMENTS = (
    (_seg_data, 14),
    (_seg_valid_frame, 14),
    (_seg_framing_bytes_in_payload, 5),
    (_seg_escaped_dle, 5),
    (_seg_not_json, 3),
    (_seg_empty_payload, 2),
    (_seg_bad_header, 3),
    (_seg_oversize, 2),
    (_seg_boundary_length, 3),
    (_seg_upper_hex_length, 2),
    (_seg_lying_length, 2),
    (_seg_split_code_point, 2),
    (_seg_deep, 1),
    (_seg_lone_dle, 3),
    (_seg_truncated_frame, 3),
)
_SEGMENT_CHOICES = [fn for fn, weight in _SEGMENTS for _ in range(weight)]


def build_stream(rng: random.Random) -> str:
    """Concatenate 1..4 random segments into one hostile stream."""
    return "".join(rng.choice(_SEGMENT_CHOICES)(rng) for _ in range(rng.randint(1, 4)))


# ---------------------------------------------------------------------------
# Adversarial splitting
# ---------------------------------------------------------------------------


def _interesting_indices(text: str) -> list[int]:
    """Offsets where a chunk boundary is most likely to break a decoder.

    Immediately after a DLE (so the escape/frame decision straddles a feed),
    and on every header field boundary of a frame that starts there.
    """
    marks: set[int] = set()
    for idx, char in enumerate(text):
        if char != DLE:
            continue
        marks.update({idx, idx + 1, idx + 2, idx + 9, idx + 10, idx + HEADER_BYTES, idx + HEADER_BYTES + 1})
    return sorted(i for i in marks if 0 < i < len(text))


def split_stream(rng: random.Random, text: str) -> list[str]:
    """Cut *text* into chunks at adversarially chosen code-point boundaries.

    Boundaries are always between code points: the decoder API is string-typed
    in all four ports, so a chunk cannot end mid-code-point. A declared length
    that lands mid-code-point is a *different* hostile case, covered by
    ``_seg_split_code_point``.
    """
    if not text:
        return [""] if rng.random() < 0.5 else []
    strategy = rng.randint(0, 3)
    if strategy == 0:
        return [text]
    if strategy == 1 and len(text) <= 48:
        return list(text)
    candidates = _interesting_indices(text) if strategy == 2 else list(range(1, len(text)))
    if not candidates:
        return [text]
    cuts = sorted(rng.sample(candidates, min(len(candidates), rng.randint(1, 4))))
    chunks = []
    previous = 0
    for cut in cuts:
        chunks.append(text[previous:cut])
        previous = cut
    chunks.append(text[previous:])
    return chunks


# ---------------------------------------------------------------------------
# Driving the reference
# ---------------------------------------------------------------------------


def _event(event: DataChunk | ControlChunk) -> dict[str, Any]:
    if isinstance(event, DataChunk):
        return {"kind": "data", "data_b64": _b64(event.data)}
    return {"kind": "control", "control": event.control}


def drive(chunks: list[str], *, finish: bool) -> dict[str, Any]:
    """Feed *chunks* to a fresh decoder and record everything it did."""
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


# ---------------------------------------------------------------------------
# Families
# ---------------------------------------------------------------------------


def _family_encode_data(rng: random.Random) -> list[dict[str, Any]]:
    cases = []
    for index in range(COUNTS["encode_data"]):
        text = rand_raw_text(rng, 32)
        cases.append({"id": f"CCF-ED-{index:04d}", "in_b64": _b64(text), "out_b64": _b64(encode_terminal_data(text))})
    return cases


def _family_encode_control(rng: random.Random) -> list[dict[str, Any]]:
    cases = []
    for index in range(COUNTS["encode_control"]):
        payload = rand_payload(rng)
        cases.append({"id": f"CCF-EC-{index:04d}", "payload": payload, "out_b64": _b64(encode_control_frame(payload))})
    return cases


def _predicate_input(rng: random.Random) -> str:
    """One structural-predicate probe: a valid frame, a mutation of one, or noise."""
    strategy = rng.randint(0, 6)
    if strategy == 0:
        return encode_control_frame(rand_payload(rng))
    if strategy == 1:
        return rand_raw_text(rng, 24)
    frame = encode_control_frame(rand_payload(rng))
    if strategy == 2:
        return frame[: rng.randint(0, len(frame))]
    if strategy == 3:
        return frame + rand_raw_text(rng, 6)
    if strategy == 4:
        cut = rng.randrange(len(frame))
        return frame[:cut] + rand_raw_char(rng) + frame[cut + 1 :]
    if strategy == 5:
        length_hex = "".join(rng.choice("0123456789abcdefABCDEFgz ") for _ in range(8))
        return DLE + STX + length_hex + rng.choice(":;") + frame[HEADER_BYTES:]
    return DLE + STX + rand_raw_text(rng, 20)


def _family_is_control_frame(rng: random.Random) -> list[dict[str, Any]]:
    cases = []
    for index in range(COUNTS["is_control_frame"]):
        text = _predicate_input(rng)
        cases.append({"id": f"CCF-PR-{index:04d}", "in_b64": _b64(text), "out": is_control_frame(text)})
    return cases


def _decode_case(case_id: str, chunks: list[str], *, finish: bool) -> dict[str, Any]:
    """Record both drives of one stream: chunk-by-chunk, and as a single feed.

    They are *not* required to agree. A chunk boundary changes where the
    decoder flushes plain data, so ``["a\\x10", "\\x10b"]`` emits two data
    events where the joined stream emits one. Recording both is what makes a
    port prove it buffers the same way, not merely that it parses the same way.
    """
    joined = "".join(chunks)
    return {
        "id": case_id,
        "chunks_b64": [_b64(chunk) for chunk in chunks],
        "finish": finish,
        "chunked": drive(chunks, finish=finish),
        "single": drive([joined], finish=finish),
    }


def _family_decode(rng: random.Random) -> list[dict[str, Any]]:
    cases = []
    for index in range(COUNTS["decode"]):
        chunks = split_stream(rng, build_stream(rng))
        cases.append(_decode_case(f"CCF-DC-{index:04d}", chunks, finish=rng.random() < 0.75))
    return cases


# Permanent regression cases. A divergence found by the weekly exploratory job
# (or by a port) is pinned here by hand, with a note saying what it caught. Ids
# are hand-assigned and never renumbered, so `CCF-REG-0002` means the same
# thing forever — unlike the generated ids, which move if a family's count or
# the seed changes.
_REGRESSIONS: tuple[tuple[str, str, list[str], bool], ...] = (
    (
        "CCF-REG-0001",
        (
            "A lone trailing DLE flushes the data before it and buffers the DLE; the "
            "next feed decides whether it was an escape or a frame. Chunked and "
            "single feeds emit different numbers of data events for this stream."
        ),
        ["a" + DLE, DLE + "b"],
        True,
    ),
    (
        "CCF-REG-0002",
        (
            "Raw DLE/STX bytes inside a control payload are covered by the length "
            "header and must not be rescanned as framing."
        ),
        [_raw_frame(f'{{"k":"{DLE}{STX}{DLE}{DLE}"}}') + "tail"],
        True,
    ),
    (
        "CCF-REG-0003",
        (
            "A frame split at every single code point, with a 3-byte code point in "
            "the payload: byte-length header vs code-point-indexed buffer walk."
        ),
        list(encode_control_frame({"k0": "你好"})),
        True,
    ),
    (
        "CCF-REG-0004",
        (
            "Found by explore_control_channel_fuzz.py. Data that precedes a frame "
            "the decoder later rejects is DELIVERED when the feed is split (an "
            "earlier feed() already returned it) and DISCARDED when the whole "
            "stream arrives at once (the raise throws away the events built so "
            "far). Same bytes, same error, different delivery."
        ),
        ["ab" + DLE + STX + "0000000c:" + '{"k"', ":1}xxxxx"],
        True,
    ),
    (
        "CCF-REG-0005",
        (
            "Found by the C# port replaying this corpus. A length header with the "
            "high bit set is an unsigned wire value; a port that accumulates it "
            "into a signed 32-bit integer wraps negative, its payload-size guard "
            "then never fires, and the negative length reaches an index or a slice "
            "— C# threw IndexOutOfRangeException where the reference reports "
            "'control payload too large'. Thirteen bytes from a peer, and the "
            "exception type is one no caller catches. The corpus missed it because "
            "every high-bit header it generated was followed by a separator other "
            "than ':', so the header was rejected as malformed before its length "
            "was ever parsed: a length is only load-bearing when ':' follows it."
        ),
        [DLE + STX + "80000000:" + '{"k":1}'],
        True,
    ),
    (
        "CCF-REG-0006",
        (
            "Found by explore_control_channel_fuzz.py, which had been reporting it "
            "on fresh seeds for a month. An UPPERCASE length header split the two "
            "readers of the same bytes: the decoder validated the field against "
            "string.hexdigits (which admits A-F) and parsed a frame, while "
            "is_control_frame() compared it against the canonical f'{n:08x}' and "
            "said the message was not framed at all. Since is_control_frame() is "
            "the gate that decides whether a payload is a control frame or "
            "terminal output, a conforming peer emitting %08X would have had its "
            "control frames rendered to the screen as text. The decoder now makes "
            "the same canonical comparison the predicate and the Go port make."
        ),
        [DLE + STX + "0000001F:" + '{"k":"' + "a" * 23 + '"}'],
        True,
    ),
)


def _family_regressions() -> list[dict[str, Any]]:
    return [
        {**_decode_case(case_id, chunks, finish=finish), "note": note} for case_id, note, chunks, finish in _REGRESSIONS
    ]


# Inputs whose *encoded* form legitimately differs between the four runtimes'
# JSON serializers. These are recorded, never asserted equal across ports: a
# port pins its own output so a change is visible in its own diff. See
# README.md. Keeping them out of the fuzz families is why those families can be
# asserted byte-for-byte.
_SERIALIZER_DIVERGENCES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("CCF-SD-0001", "CPython keeps int/float apart through JSON; Go, .NET and JS all write 0.", {"k0": 0.0}),
    ("CCF-SD-0002", "Same, inside an array, next to a value that really is fractional.", {"k0": [1.0, 1.5, 2]}),
    ("CCF-SD-0003", "Go and .NET escape U+2028/U+2029; CPython and JS emit them raw.", {"k0": "\u2028\u2029"}),
    ("CCF-SD-0004", ".NET escapes DEL (U+007F); the other three emit it raw.", {"k0": "\u007f"}),
    (
        "CCF-SD-0005",
        ".NET writes \\uXXXX escapes with upper-case hex digits (\\u001F); the other three use lower case.",
        {"k0": "\u001f"},
    ),
    (
        "CCF-SD-0006",
        ".NET writes astral code points as a \\uXXXX surrogate pair; CPython, Go and JS emit raw UTF-8.",
        {"k0": "\U0001d11e"},
    ),
)


def _family_serializer_divergences() -> list[dict[str, Any]]:
    return [
        {"id": case_id, "note": note, "payload": payload, "cpython_out_b64": _b64(encode_control_frame(payload))}
        for case_id, note, payload in _SERIALIZER_DIVERGENCES
    ]


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


def build_corpus(seed: int) -> dict[str, Any]:
    """Build the whole corpus deterministically from *seed*."""
    rng = random.Random(seed)
    families = {
        "encode_data": _family_encode_data(rng),
        "encode_control": _family_encode_control(rng),
        "is_control_frame": _family_is_control_frame(rng),
        "decode": _family_decode(rng),
        "regressions": _family_regressions(),
        "serializer_divergences": _family_serializer_divergences(),
    }
    return {
        "schema": SCHEMA,
        "generator": "conformance/fuzz/gen_control_channel_fuzz.py",
        "reference": "CPython provide.uterm.control_channel",
        "seed": seed,
        "limits": {
            "header_bytes": HEADER_BYTES,
            "max_control_payload_bytes": _MAX_CONTROL_PAYLOAD_BYTES,
            "max_frame_depth": _MAX_CONTROL_FRAME_DEPTH,
        },
        "counts": {name: len(cases) for name, cases in families.items()},
        **families,
    }


def render(corpus: dict[str, Any]) -> str:
    """Serialize the corpus as pure-ASCII JSON.

    ``ensure_ascii=True`` is deliberate and load-bearing: every string in the
    document becomes ASCII (base64, or ``\\uXXXX`` escapes inside a recorded
    payload), so no reader has to agree with CPython about file encoding,
    normalization, or which code points a JSON string literal may carry raw.
    """
    text = json.dumps(corpus, indent=1, ensure_ascii=True, sort_keys=False) + "\n"
    if not text.isascii():  # pragma: no cover — guards the invariant above
        raise AssertionError("corpus must be pure ASCII")
    return text


def main(argv: list[str] | None = None) -> int:
    """Write the corpus and report what it contains."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=CORPUS_SEED, help=f"generator seed (default {CORPUS_SEED})")
    parser.add_argument("--out", type=Path, default=OUT, help="output path")
    args = parser.parse_args(argv)

    corpus = build_corpus(args.seed)
    args.out.write_text(render(corpus), encoding="utf-8")
    total = sum(corpus["counts"].values())
    print(f"wrote {args.out} (seed={args.seed}, {total} cases: {corpus['counts']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
