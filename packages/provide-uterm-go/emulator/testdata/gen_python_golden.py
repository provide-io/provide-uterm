#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the emulator parity golden from the REAL Python emulator
(provide.uterm.emulator.TerminalEmulator). Run from the repo root:

    uv run python packages/provide-uterm-go/emulator/testdata/gen_python_golden.py

Writes python_golden.json next to this script. emulator_test.go's
TestGoldenParityWithPython replays each raw_hex through the Go emulator and
asserts every field below matches, so a divergence in either implementation
fails CI.

Geometry is 40x6 with the default ANSI term, matching the Go test's
`New(40, 6, "")` — Go substitutes "ANSI" for an empty term, which is also
Python's default, so the two agree without either side passing it explicitly.

The INPUTS are fixed; the OUTPUTS are recomputed every run. That is the point:
if pyte, the CP437 decode, the screen-hash, or the cursor-at-end heuristic ever
changes, this file moves and .ci/check_goldens.sh fails.
"""

from __future__ import annotations

import json
import pathlib

from provide.uterm.emulator import TerminalEmulator

COLS, ROWS = 40, 6

# Four streams chosen to exercise different paths, recorded as hex so the raw
# bytes survive round-tripping through JSON:
#   1. plain text plus a BBS-style prompt ending in ": " (drives has_trailing_space)
#   2. CSI erase/home + SGR colour runs (drives the ANSI row-0 rendering)
#   3. CP437 box-drawing bytes (drives the non-UTF-8 decode)
#   4. a stream with no prompt at all, ending in CRLF (cursor lands on a fresh row)
RAW_HEX = [
    "48656c6c6f2c2042425320576f726c64210d0a436f6d6d616e64205b544c3d30303a30303a30305d3a5b333330355d20283f3d48656c70293f203a20",
    "1b5b324a1b5b481b5b313b33316d5245441b5b306d206e6f726d616c0d0a70726f6d70743e20",
    "c9cdcdbb0d0ac8cdcdbc0d0a456e74657220796f75722063686f6963653a20",
    "6e6f2070726f6d707420686572650d0a",
]


def case(raw_hex: str) -> dict[str, object]:
    emulator = TerminalEmulator(cols=COLS, rows=ROWS)
    emulator.process(bytes.fromhex(raw_hex))
    snapshot = emulator.get_snapshot()
    return {
        "raw_hex": raw_hex,
        "screen": snapshot["screen"],
        "hash": snapshot["screen_hash"],
        "cursor": snapshot["cursor"],
        "cae": snapshot["cursor_at_end"],
        "hts": snapshot["has_trailing_space"],
        "raw_tail": snapshot["raw_tail"],
        # Only row 0: the Go test compares SplitN(ANSIScreen(), "\n", 2)[0].
        "ansi0": emulator.ansi_screen().split("\n", 1)[0],
    }


def main() -> None:
    cases = [case(raw_hex) for raw_hex in RAW_HEX]
    out = pathlib.Path(__file__).with_name("python_golden.json")
    # Single-line json.dumps defaults, NOT the indent=2 other corpora use —
    # matching whatever each corpus was recorded with is what keeps the
    # regenerate-and-compare check meaningful.
    out.write_text(json.dumps(cases) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(cases)} cases)")


if __name__ == "__main__":
    main()
