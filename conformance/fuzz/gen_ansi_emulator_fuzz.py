#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the cross-language differential fuzz corpus for the ANSI layer and the emulator.

Companion to ``gen_control_channel_fuzz.py``; read
``conformance/fuzz/README.md`` for the shared format rules before changing
anything here. Same contract: deterministic from an integer seed, all inputs
carried as base64 of UTF-8, whole document ASCII, every case grep-able by id.

Why this surface. The ANSI parser and the emulator are the largest parsing
surface in the product and the one consuming genuinely untrusted bytes —
terminal output from whatever the session is running. Before this corpus they
had **no** generative testing in any of the four ports, and cross-language
agreement rested on nineteen hand-written emulator cases and a single
twenty-seven-byte input in the shared Go/C# vector file.

Two kinds of family, and the distinction matters:

* ``normalize`` / ``upgrade_256`` / ``upgrade_truecolor`` are pure string
  transforms over SGR sequences. Nothing but the port's own code decides the
  answer, so a divergence is unambiguously a port bug.
* ``emulator`` drives a real terminal emulator. The reference's is built on
  **pyte**, so parity here means the other ports reproducing pyte's semantics —
  which they already claim for the nineteen recorded cases. A divergence found
  by a generated stream is therefore a genuine finding, but it may be a
  disagreement about an obscure corner of a third-party implementation rather
  than a defect anybody chose. Recorded as the reference's answer either way,
  and worth reading the note on a failure before assuming the port is wrong.

Usage (from the repository root)::

    uv run python conformance/fuzz/gen_ansi_emulator_fuzz.py
    uv run python conformance/fuzz/gen_ansi_emulator_fuzz.py --seed 7 --out /tmp/x.json
"""

from __future__ import annotations

import argparse
import base64
import itertools
import json
import random
import sys
from pathlib import Path
from typing import Any, Final

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "packages" / "provide-uterm" / "src"))

from provide.uterm.ansi import (  # noqa: E402
    normalize_colors,
    upgrade_to_256,
    upgrade_to_truecolor,
)
from provide.uterm.emulator import TerminalEmulator  # noqa: E402

SCHEMA = "provide-uterm/ansi-emulator-fuzz/1"
#: The committed corpus is generated from this seed. CI regenerates with it and
#: fails on any difference, so every port is held to identical inputs.
CORPUS_SEED = 20260730
OUT = Path(__file__).with_name("ansi_emulator_fuzz.json")

ESC = "\x1b"

# How many cases each family contributes. Weighted toward the emulator: it is
# the only *stateful* surface here, so it is the only one where a port can be
# right about every individual sequence and still end up with a different
# screen.
COUNTS: Final = {"normalize": 112, "upgrade_256": 96, "upgrade_truecolor": 96, "emulator": 128}

# Emulator geometry. Small on purpose: a narrow screen makes wrapping, scrolling
# and cursor clamping happen within a few characters instead of needing a
# hundred, so the generated streams actually reach those paths.
EMU_COLS: Final = 20
EMU_ROWS: Final = 6


def _b64(text: str) -> str:
    """Encode *text* as base64 of its UTF-8 bytes."""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------------------
# SGR / escape-sequence builders
# ---------------------------------------------------------------------------

#: SGR parameters worth generating, by what they exercise rather than by value.
#: The 30-37/40-47 and 90-97 ranges are what the colour upgraders rewrite; 38/48
#: introduce the extended forms with their own sub-grammar; 0-9 are the
#: attributes that must survive an upgrade untouched.
_SGR_PARAMS: Final = (
    [str(n) for n in range(10)]
    + [str(n) for n in range(30, 38)]
    + [str(n) for n in range(40, 48)]
    + [str(n) for n in range(90, 98)]
    + [str(n) for n in range(100, 108)]
    + ["39", "49", "1", "22", "", "0", "00", "000"]  # leading zeros and empty params
)

#: Extended-colour forms, including the ones with a wrong argument count. A
#: parser that trusts the declared form and reads past the parameters it was
#: given is the failure these are for.
_EXTENDED_SGR: Final = (
    "38;5;0",
    "38;5;15",
    "38;5;231",
    "38;5;255",
    "38;5;256",  # out of range
    "38;5",  # truncated
    "38;5;",
    "38;2;0;0;0",
    "38;2;255;255;255",
    "38;2;300;0;0",  # out of range component
    "38;2;1;2",  # truncated
    "48;5;196",
    "48;2;10;20;30",
    "38;9;1",  # undefined colour space
    "38",
    "48",
)


def _sgr(rng: random.Random) -> str:
    """One SGR sequence, sometimes with several parameters."""
    if rng.random() < 0.25:
        return f"{ESC}[{rng.choice(_EXTENDED_SGR)}m"
    count = rng.randint(1, 4)
    return f"{ESC}[{';'.join(rng.choice(_SGR_PARAMS) for _ in range(count))}m"


def _malformed_escape(rng: random.Random) -> str:
    """An escape sequence that is wrong in one specific way.

    Each of these is a shape a parser can mis-handle by scanning too far or not
    far enough: an ESC with nothing after it, a CSI that never terminates, a
    parameter list with a letter in it, a private-mode intermediate, an OSC with
    no terminator, and a DCS the parser must skip whole.
    """
    return rng.choice(
        (
            ESC,
            f"{ESC}[",
            f"{ESC}[999999999999m",  # parameter far past any sane bound
            f"{ESC}[1;2;3;4;5;6;7;8;9;10;11;12m",  # more parameters than anyone reads
            f"{ESC}[3x1m",  # letter inside the parameters
            f"{ESC}[?25h",  # private mode
            f"{ESC}[>4;2m",
            f"{ESC}]0;a title with no terminator",
            f"{ESC}]8;;https://example.invalid{ESC}\\",  # OSC 8 hyperlink, properly closed
            f"{ESC}P1;2q data {ESC}\\",  # DCS
            f"{ESC}[38;5;1",  # extended colour cut off mid-sequence
            f"{ESC}%G",  # charset selection
            f"{ESC}(B",
            f"{ESC}[m",  # empty SGR: a reset by omission
        )
    )


def _text_run(rng: random.Random) -> str:
    """Printable payload, including the widths that break column arithmetic."""
    pool = rng.choice(
        (
            "abcdefghijklmnopqrstuvwxyz ",
            "0123456789",
            "áéíóúñçüö",  # two UTF-8 bytes, one column
            "你好世界",  # three bytes, two columns each
            "𝄞😀🜁",  # four bytes, two UTF-16 units
            "─│┌┐└┘├┤",  # box drawing, the CP437 path
            "\t\r\n",
            "\x00\x01\x07\x08\x0b\x0c\x0e\x1f\x7f",  # C0 controls and DEL
        )
    )
    return "".join(rng.choice(pool) for _ in range(rng.randint(1, 12)))


def _cursor_move(rng: random.Random) -> str:
    """Cursor motion, including moves that aim off the screen.

    A terminal clamps rather than wrapping or erroring, and the clamp is exactly
    the kind of edge two implementations disagree about.
    """
    verb = rng.choice("ABCDEFGHJKLMPSTdfsu")
    if rng.random() < 0.3:
        return f"{ESC}[{verb}"  # no parameter: the default
    first = rng.choice((0, 1, 2, 3, 5, EMU_ROWS, EMU_ROWS + 5, EMU_COLS + 10, 999))
    if rng.random() < 0.5:
        return f"{ESC}[{first}{verb}"
    second = rng.choice((0, 1, 2, EMU_COLS, EMU_COLS + 3, 999))
    return f"{ESC}[{first};{second}{verb}"


def _ansi_stream(rng: random.Random) -> str:
    """A hostile mixture of text, SGR, cursor motion and malformed escapes."""
    parts: list[str] = []
    for _ in range(rng.randint(1, 8)):
        roll = rng.random()
        if roll < 0.4:
            parts.append(_text_run(rng))
        elif roll < 0.65:
            parts.append(_sgr(rng))
        elif roll < 0.85:
            parts.append(_cursor_move(rng))
        else:
            parts.append(_malformed_escape(rng))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Pure-transform families
# ---------------------------------------------------------------------------


def _family_normalize(rng: random.Random) -> list[dict[str, Any]]:
    """``normalize_colors`` over generated SGR text.

    A pure string transform, so nothing but the port's own code decides the
    answer and a divergence is unambiguously a port bug.
    """
    return [
        {"id": f"AEF-NM-{index:04d}", "in_b64": _b64(text), "out_b64": _b64(normalize_colors(text))}
        for index, text in enumerate((_ansi_stream(rng) for _ in range(COUNTS["normalize"])), start=1)
    ]


def _family_upgrade_256(rng: random.Random) -> list[dict[str, Any]]:
    """``upgrade_to_256`` over generated SGR text."""
    return [
        {"id": f"AEF-U2-{index:04d}", "in_b64": _b64(text), "out_b64": _b64(upgrade_to_256(text))}
        for index, text in enumerate((_ansi_stream(rng) for _ in range(COUNTS["upgrade_256"])), start=1)
    ]


def _family_upgrade_truecolor(rng: random.Random) -> list[dict[str, Any]]:
    """``upgrade_to_truecolor`` over generated SGR text."""
    return [
        {"id": f"AEF-UT-{index:04d}", "in_b64": _b64(text), "out_b64": _b64(upgrade_to_truecolor(text))}
        for index, text in enumerate((_ansi_stream(rng) for _ in range(COUNTS["upgrade_truecolor"])), start=1)
    ]


# ---------------------------------------------------------------------------
# Emulator family
# ---------------------------------------------------------------------------


def _drive(chunks: list[str]) -> dict[str, Any]:
    """Feed *chunks* to a fresh emulator and record what it shows."""
    emulator = TerminalEmulator(cols=EMU_COLS, rows=EMU_ROWS)
    for chunk in chunks:
        emulator.process(chunk.encode("utf-8"))
    snapshot = emulator.get_snapshot()
    return {
        # The screen is the product: what a person or an agent would read.
        "screen_b64": _b64(str(snapshot["screen"])),
        "cursor": {"x": snapshot["cursor"]["x"], "y": snapshot["cursor"]["y"]},
        "cols": snapshot["cols"],
        "rows": snapshot["rows"],
        "cursor_at_end": snapshot["cursor_at_end"],
        "has_trailing_space": snapshot["has_trailing_space"],
        # The styled rendering, which is where an SGR disagreement surfaces even
        # when the plain text agrees.
        "ansi_screen_b64": _b64(emulator.ansi_screen()),
    }


def _emulator_case(case_id: str, chunks: list[str]) -> dict[str, Any]:
    """One emulator case, driven twice.

    Chunked and whole are recorded separately and are **not** required to agree.
    An emulator holds partial escape sequences across a feed, so where a chunk
    boundary falls inside one decides what the screen shows — the same trap the
    control-frame corpus found, on a different surface.
    """
    return {
        "id": case_id,
        "chunks_b64": [_b64(chunk) for chunk in chunks],
        "chunked": _drive(chunks),
        "single": _drive(["".join(chunks)]),
    }


def _split(rng: random.Random, text: str) -> list[str]:
    """Cut *text* at boundaries chosen to land inside escape sequences.

    Splitting at random byte offsets would mostly cut between characters, which
    is the uninteresting case. Aiming at ESC and ``[`` puts the boundary where a
    parser has to remember what it was in the middle of.
    """
    if not text:
        return []
    cuts: set[int] = set()
    for index, char in enumerate(text):
        if char in (ESC, "[", ";", "m") and rng.random() < 0.4:
            cuts.add(min(index + rng.randint(0, 2), len(text)))
    while len(cuts) < 2 and len(text) > 2:
        cuts.add(rng.randint(1, len(text) - 1))
    ordered = [0, *sorted(cuts), len(text)]
    return [text[start:end] for start, end in itertools.pairwise(ordered) if start < end]


def _family_emulator(rng: random.Random) -> list[dict[str, Any]]:
    """Generated streams through the emulator, chunked and whole."""
    cases = []
    for index in range(1, COUNTS["emulator"] + 1):
        stream = _ansi_stream(rng)
        cases.append(_emulator_case(f"AEF-EM-{index:04d}", _split(rng, stream)))
    return cases


# Permanent regression cases. A divergence found by the exploratory job or by a
# port is pinned here by hand with a note. Ids are never renumbered, so
# ``AEF-REG-0001`` means the same thing forever.
_REGRESSIONS: Final[tuple[tuple[str, str, list[str]], ...]] = (
    (
        "AEF-REG-0001",
        (
            "An SGR split across a feed boundary: the parameters arrive in one chunk "
            "and the terminating 'm' in the next. A port that flushes on a chunk "
            "boundary rather than holding the partial sequence prints the parameters "
            "as text."
        ),
        [f"{ESC}[3", "1mred"],
    ),
    (
        "AEF-REG-0002",
        (
            "A wide code point that straddles the last column: the screen is twenty "
            "columns and the character needs two, so it either wraps whole or is "
            "split. Column arithmetic that counts UTF-8 bytes, or UTF-16 units, "
            "rather than display width disagrees here."
        ),
        ["x" * 19 + "你好"],
    ),
    (
        "AEF-REG-0003",
        (
            "An extended colour cut off mid-sequence, then more text. The parser has "
            "to decide how much to discard: too little and the leftover parameters "
            "print, too much and the following text vanishes."
        ),
        [f"{ESC}[38;5;", "1mtail"],
    ),
)


def _family_regressions() -> list[dict[str, Any]]:
    return [{**_emulator_case(case_id, chunks), "note": note} for case_id, note, chunks in _REGRESSIONS]


def build_corpus(seed: int) -> dict[str, Any]:
    """Build the whole corpus deterministically from *seed*."""
    rng = random.Random(seed)
    families = {
        "normalize": _family_normalize(rng),
        "upgrade_256": _family_upgrade_256(rng),
        "upgrade_truecolor": _family_upgrade_truecolor(rng),
        "emulator": _family_emulator(rng),
        "regressions": _family_regressions(),
    }
    return {
        "schema": SCHEMA,
        "generator": "conformance/fuzz/gen_ansi_emulator_fuzz.py",
        "reference": "CPython provide.uterm.ansi + provide.uterm.emulator (pyte)",
        "seed": seed,
        "geometry": {"cols": EMU_COLS, "rows": EMU_ROWS},
        "counts": {name: len(cases) for name, cases in families.items()},
        **families,
    }


def render(corpus: dict[str, Any]) -> str:
    """Serialize the corpus as pure-ASCII JSON.

    ``ensure_ascii=True`` is load-bearing for the same reason as in the
    control-channel corpus: every string becomes ASCII, so no reader has to
    agree with CPython about file encoding or which code points a JSON string
    literal may carry raw.
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
