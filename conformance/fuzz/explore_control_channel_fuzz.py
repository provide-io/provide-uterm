#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Explore the control-frame codec with a fresh seed, looking for new failures.

This is the *other half* of the fuzz corpus, and it is deliberately not the
committed one. ``control_channel_fuzz.json`` is frozen: every port replays the
same inputs forever, and a corpus that shifted under a port's feet would be
useless as a contract. So the committed corpus can never find anything new.

This script can. It draws a **fresh random seed** every run, generates inputs
from the same builders, and checks the CPython reference against *itself* —
properties that must hold for any input, which is the only kind of check
available without a second implementation to differ from.

On a failure it prints the seed and the base64 of every chunk it fed, in feed
order — the exact form that goes into ``_REGRESSIONS`` in the generator. That is
the workflow: exploration finds it, a human pins it, it becomes permanent.

Usage::

    uv run python conformance/fuzz/explore_control_channel_fuzz.py
    uv run python conformance/fuzz/explore_control_channel_fuzz.py --seed 1234 --iterations 50000
"""

from __future__ import annotations

import argparse
import base64
import random
import secrets
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gen_control_channel_fuzz import (
    _b64,
    build_stream,
    drive,
    rand_payload,
    rand_raw_text,
    split_stream,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages" / "provide-uterm" / "src"))

from provide.uterm.control_channel import (
    ControlChunk,
    ControlFrameDecoder,
    ControlFrameProtocolError,
    DataChunk,
    encode_control_frame,
    encode_terminal_data,
    is_control_frame,
)


class DivergenceError(Exception):
    """A property the reference must satisfy did not hold.

    Carries the exact chunks that were fed. Re-deriving them from the RNG after
    the fact is how a fuzzer ends up printing an input that is not the one that
    failed, so the input travels with the exception instead.
    """

    def __init__(self, message: str, chunks: list[str]) -> None:
        super().__init__(message)
        self.chunks = chunks


def _decode_all(text: str) -> list[DataChunk | ControlChunk]:
    decoder = ControlFrameDecoder()
    events = list(decoder.feed(text))
    events.extend(decoder.finish())
    return events


def _logical(record: dict[str, Any]) -> tuple[bytes, list[Any]]:
    """The stream's *meaning*: joined terminal data and ordered control payloads.

    Event boundaries legitimately move when a feed is split, so comparing
    ``events`` lists between a chunked and a single feed would report false
    positives. What must never move for an accepted stream is the concatenated
    terminal data and the sequence of control payloads.
    """
    # Decode before joining: base64 is not concatenative, so joining the encoded
    # forms would report a false divergence whenever the two feeds split the
    # same bytes across a different number of data events — which is the norm.
    data = b"".join(base64.b64decode(e["data_b64"]) for e in record["events"] if e["kind"] == "data")
    control = [e["control"] for e in record["events"] if e["kind"] == "control"]
    return data, control


def check_terminal_data_round_trip(rng: random.Random) -> None:
    """encode_terminal_data -> decode must give the payload back unchanged."""
    payload = rand_raw_text(rng, 64)
    events = _decode_all(encode_terminal_data(payload))
    if any(isinstance(e, ControlChunk) for e in events):
        raise DivergenceError("encoded terminal data decoded as a control frame", [payload])
    joined = "".join(e.data for e in events if isinstance(e, DataChunk))
    if joined != payload:
        raise DivergenceError(f"terminal data round-trip lost bytes: {_b64(joined)} != {_b64(payload)}", [payload])


def check_control_frame_round_trip(rng: random.Random) -> None:
    """encode_control_frame -> decode must give exactly one identical payload."""
    payload = rand_payload(rng)
    frame = encode_control_frame(payload)
    events = _decode_all(frame)
    if len(events) != 1 or not isinstance(events[0], ControlChunk):
        raise DivergenceError(f"a single frame decoded to {len(events)} events", [frame])
    if events[0].control != payload:
        raise DivergenceError("control payload changed across a round trip", [frame])
    if not is_control_frame(frame):
        raise DivergenceError("is_control_frame rejected a frame the encoder produced", [frame])


def check_never_raises_anything_else(rng: random.Random) -> None:
    """Arbitrary input parses or raises ControlFrameProtocolError. Nothing else.

    On rejection the decoder must also have dropped its buffer, or the next feed
    resumes inside a half-parsed frame.
    """
    text = build_stream(rng)
    decoder = ControlFrameDecoder()
    try:
        decoder.feed(text)
        decoder.finish()
    except ControlFrameProtocolError:
        if decoder._buffer or decoder._buffer_parts:
            raise DivergenceError("decoder kept buffered state after a protocol error", [text]) from None
    except Exception as exc:
        raise DivergenceError(f"decoder raised {type(exc).__name__}: {exc}", [text]) from exc


def check_chunked_matches_single(rng: random.Random) -> None:
    """A split feed and a whole feed must reject the same streams, identically.

    For an *accepted* stream they must also agree on its meaning. For a rejected
    one they need not: a chunked feed can deliver the data that preceded the bad
    frame (a previous ``feed()`` already returned it) where a single feed
    discards it with the raise. That difference is real and is recorded
    case-by-case in the corpus — see ``CCF-REG-0004`` — so it is not a property
    violation here.
    """
    text = build_stream(rng)
    chunks = split_stream(rng, text)
    joined = "".join(chunks)
    chunked = drive(chunks, finish=True)
    single = drive([joined], finish=True)
    if chunked["error"] != single["error"]:
        raise DivergenceError(f"chunked error {chunked['error']!r} != single error {single['error']!r}", chunks)
    if chunked["error"] is None and _logical(chunked) != _logical(single):
        raise DivergenceError(f"chunked feed disagrees with single feed ({len(chunks)} chunks)", chunks)


def check_predicate_agrees_with_decoder(rng: random.Random) -> None:
    """A stream that decodes to exactly one control frame must satisfy the predicate.

    The converse does not hold — the predicate is structural and says nothing
    about whether the payload is JSON — so only this direction is checked.
    """
    text = build_stream(rng)
    try:
        events = _decode_all(text)
    except ControlFrameProtocolError:
        return
    if len(events) == 1 and isinstance(events[0], ControlChunk) and not is_control_frame(text):
        raise DivergenceError("decoder consumed the whole stream as one frame but the predicate said no", [text])


CHECKS = (
    check_terminal_data_round_trip,
    check_control_frame_round_trip,
    check_never_raises_anything_else,
    check_chunked_matches_single,
    check_predicate_agrees_with_decoder,
)


def explore(seed: int, iterations: int) -> int:
    """Run every check *iterations* times. Returns a process exit code."""
    rng = random.Random(seed)
    for iteration in range(iterations):
        for check in CHECKS:
            try:
                check(rng)
            except DivergenceError as exc:
                _report(seed, iteration, check.__name__, exc)
                return 1
    print(f"OK: {len(CHECKS)} properties x {iterations} iterations held (seed={seed})")
    return 0


def _report(seed: int, iteration: int, name: str, exc: DivergenceError) -> None:
    """Print the seed and the offending chunks in pin-it-as-a-regression form."""
    print("=" * 72)
    print(f"DIVERGENCE  seed={seed}  iteration={iteration}  check={name}")
    print(f"  {exc}")
    print("  offending chunks (base64 of UTF-8, in feed order):")
    for index, chunk in enumerate(exc.chunks):
        print(f"    [{index}] {_b64(chunk)}")
    print()
    print("  Pin it: add to _REGRESSIONS in conformance/fuzz/gen_control_channel_fuzz.py,")
    print("  take the next CCF-REG-nnnn, regenerate the corpus, and commit both.")
    print(f"  Reproduce: uv run python conformance/fuzz/{Path(__file__).name} --seed {seed}")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and explore."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=None, help="seed (default: a fresh random one)")
    parser.add_argument("--iterations", type=int, default=20_000, help="iterations per property")
    args = parser.parse_args(argv)
    seed = secrets.randbelow(2**31) if args.seed is None else args.seed
    print(f"exploring control_channel with seed={seed}, iterations={args.iterations}", flush=True)
    return explore(seed, args.iterations)


if __name__ == "__main__":
    raise SystemExit(main())
