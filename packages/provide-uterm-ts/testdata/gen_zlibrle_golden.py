#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Record what CPython's zlib emits at level 9 with the run-length strategy.

The Go port cannot ask its standard library for this stream — ``compress/flate``
exposes no strategy and builds its own trees — so it ports the parts of zlib
that produce it (``packages/provide-uterm-go/gui/zlibrle.go``). A port is only
worth having if it is checked against the real thing on more than the handful
of screenshots the GUI corpus happens to contain, because the ways it can
diverge are all invisible until they bite: a tie broken the other way when two
symbols share a frequency, a block split at a different symbol, a tie between
the static and dynamic trees resolved the other way.

So the cases here are chosen to reach those decisions rather than to look like
screenshots:

* ``runs`` walks the run-length matcher through every match length around the
  3-byte minimum and the 258-byte maximum.
* ``noise`` is nearly all literals, which is what fills a block quickly — the
  larger sizes cross the 16383-symbol boundary where zlib starts a new block.
* ``sparse`` mixes long runs with occasional noise, so the two trees carry very
  different symbol distributions.
* ``tiny`` covers the inputs small enough that the static tree wins, including
  the empty one.

Every buffer is generated from a rule the Go test reproduces exactly, so the
corpus stores only the rule and the digest rather than megabytes of bytes.
"""

from __future__ import annotations

import hashlib
import json
import zlib
from pathlib import Path

OUT = Path(__file__).parent / "zlibrle_golden.json"

# A tiny LCG, so both languages generate identical buffers from a seed.
LCG_MULT = 1103515245
LCG_ADD = 12345
LCG_MOD = 1 << 31


def lcg_bytes(seed: int, size: int) -> bytes:
    out = bytearray(size)
    state = seed
    for i in range(size):
        state = (state * LCG_MULT + LCG_ADD) % LCG_MOD
        out[i] = (state >> 16) & 0xFF
    return bytes(out)


def build(kind: str, seed: int, size: int) -> bytes:
    if kind == "noise":
        return lcg_bytes(seed, size)
    if kind == "runs":
        # Run lengths cycle through the interesting boundaries: below the
        # 3-byte minimum match, just over it, and around the 258-byte cap.
        lengths = [1, 2, 3, 4, 5, 257, 258, 259, 260, 7, 128]
        out = bytearray()
        state = seed
        index = 0
        while len(out) < size:
            state = (state * LCG_MULT + LCG_ADD) % LCG_MOD
            value = (state >> 16) & 0xFF
            out.extend(bytes([value]) * lengths[index % len(lengths)])
            index += 1
        return bytes(out[:size])
    if kind == "sparse":
        out = bytearray()
        state = seed
        while len(out) < size:
            state = (state * LCG_MULT + LCG_ADD) % LCG_MOD
            value = (state >> 16) & 0xFF
            if value & 1:
                out.extend(b"\x00" * (value * 4 + 3))
            else:
                out.extend(bytes([value]) * 2)
        return bytes(out[:size])
    raise ValueError(f"unknown kind: {kind}")


def compress(raw: bytes) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, 15, 8, zlib.Z_RLE)
    return compressor.compress(raw) + compressor.flush()


def main() -> int:
    cases: list[dict[str, object]] = []

    # Small enough that the static tree usually wins, plus the empty input.
    for size in (0, 1, 2, 3, 4, 5, 16, 100, 255, 256, 257, 258, 259, 1000):
        raw = bytes(size)
        cases.append(
            {
                "name": f"zeros/{size}",
                "kind": "zeros",
                "seed": 0,
                "size": size,
                "length": len(compress(raw)),
                "sha256": hashlib.sha256(compress(raw)).hexdigest(),
            }
        )

    for kind in ("runs", "noise", "sparse"):
        # 40000 and 200000 both cross the 16383-symbol block boundary; 200000
        # also crosses zlib's 64 KiB window.
        for size in (64, 1024, 20000, 40000, 200000):
            for seed in (1, 7):
                raw = build(kind, seed, size)
                encoded = compress(raw)
                cases.append(
                    {
                        "name": f"{kind}/{size}/{seed}",
                        "kind": kind,
                        "seed": seed,
                        "size": size,
                        "raw_sha256": hashlib.sha256(raw).hexdigest(),
                        "length": len(encoded),
                        "sha256": hashlib.sha256(encoded).hexdigest(),
                    }
                )

    payload = {
        "note": "CPython zlib level 9, Z_RLE strategy, windowBits 15, memLevel 8",
        "lcg": {"mult": LCG_MULT, "add": LCG_ADD, "mod": LCG_MOD},
        "cases": cases,
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(cases)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
