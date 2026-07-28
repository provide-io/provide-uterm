#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript ``ansi`` port.

Covers the BBS colour-token dialects, the 16-colour upgrade paths, and the
palette tables. Every dialect handler is a pure text transform, so the corpus
sweeps each one exhaustively over its own token space rather than sampling.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_ansi_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm._ansi_dialects import (
    _handle_brace_tokens,
    _handle_extended_tokens,
    _handle_pipe_codes,
    _handle_tilde_codes,
)

from provide.uterm.ansi import (
    BOLD,
    CLEAR_SCREEN,
    DEFAULT_PALETTE,
    DEFAULT_RGB,
    RESET,
    _color256_to_rgb,
    normalize_colors,
    registered_dialects,
    upgrade_to_256,
    upgrade_to_truecolor,
)

OUT = Path(__file__).with_name("ansi_golden.json")


def _extended_token_inputs() -> list[str]:
    """Every {F}/{B}/{P}/{T} token shape, plus the malformed neighbours."""
    inputs = ["", "no tokens here"]
    # Sweep the whole one-to-three digit space at the interesting boundaries.
    for value in (0, 1, 7, 8, 9, 15, 16, 31, 99, 100, 128, 200, 255):
        for kind in "FBPT":
            inputs.append(f"{{{kind}{value}}}")
    # Values above the lookup table fall back to a computed escape.
    inputs.extend(["{F256}", "{B256}", "{F999}", "{B999}", "{P999}", "{T999}"])
    # Leading zeros keep their numeric value.
    inputs.extend(["{F007}", "{P015}", "{B00}"])
    # Malformed tokens must survive verbatim.
    inputs.extend(["{F}", "{F1234}", "{Z1}", "{f1}", "{F-1}", "{F 1}", "{", "}", "{}"])
    # Tokens embedded in text, adjacent, and repeated.
    inputs.extend(
        [
            "before{F196}after",
            "{F196}{B21}both",
            "{P1}{P2}{P3}",
            "a{F1}b{B2}c{P3}d{T4}e",
        ]
    )
    return inputs


def _tilde_inputs() -> list[str]:
    """Every mapped tilde code, plus unmapped and edge cases."""
    mapped = ["1", "2", "3", "4", "5", "6", "7", "0"]
    letters = ["r", "R", "g", "G", "y", "Y", "b", "B", "m", "M", "c", "C", "w", "W", "d", "D", "E"]
    inputs = ["", "no tildes"]
    inputs.extend(f"~{code}" for code in mapped + letters)
    # Unmapped codes are re-emitted with their tilde.
    inputs.extend(["~z", "~8", "~9", "~~", "~ ", "~{"])
    # A trailing tilde has nothing to consume.
    inputs.extend(["~", "text~"])
    # The pattern is not DOTALL, so a tilde before a newline does not match.
    inputs.extend(["~\n", "a~\nb"])
    inputs.extend(["a~rb~gc", "~r~g~y", "before~wafter"])
    return inputs


def _brace_inputs() -> list[str]:
    """Every mapped brace token, the TWGS header token, and near-misses."""
    tokens = [
        "{+c}", "{-c}", "{+r}", "{-r}", "{+g}", "{-g}", "{+y}", "{-y}",
        "{+b}", "{-b}", "{+m}", "{-m}", "{+w}", "{-w}", "{+k}", "{-k}",
        "{-x}", "{NK}", "{T}", "{t}", "{+Bw}",
    ]  # fmt: skip
    inputs = ["", "no braces"]
    inputs.extend(tokens)
    # The four-character token is matched before the three-character ones.
    inputs.extend(["{-Bw}", "{+Bw}{+c}", "{+c}{+Bw}"])
    # Unmapped single-character tags pass through the split unchanged.
    inputs.extend(["{+z}", "{-z}", "{+C}", "{nk}", "{NKX}"])
    inputs.extend(["a{+c}b{-x}c", "{+r}{+g}{+b}"])
    return inputs


def _pipe_inputs() -> list[str]:
    """The full |00-|23 range plus out-of-range and malformed codes."""
    inputs = ["", "no pipes"]
    inputs.extend(f"|{value:02d}" for value in range(24))
    # Beyond the table, the code is re-emitted with its pipe.
    inputs.extend([f"|{value:02d}" for value in (24, 25, 30, 99)])
    # Malformed: one digit, three digits, non-digits, a bare pipe.
    inputs.extend(["|0", "|123", "|ab", "|", "a|b", "||07"])
    inputs.extend(["|07text|00", "|01|02|03"])
    return inputs


def _upgrade_inputs() -> list[str]:
    """SGR sequences and palette tokens for the two upgrade paths."""
    inputs = ["", "plain text"]
    # Every foreground and background code, dim and bright.
    for code in list(range(30, 38)) + list(range(90, 98)) + list(range(40, 48)) + list(range(100, 108)):
        inputs.append(f"\x1b[{code}mX")
    # Bold promotes a dim foreground to its bright entry, but not a background.
    inputs.extend(["\x1b[1;31mX", "\x1b[1;41mX", "\x1b[1;91mX", "\x1b[31;1mX"])
    # Non-colour parameters pass through in place.
    inputs.extend(["\x1b[0mX", "\x1b[1mX", "\x1b[4;31mX", "\x1b[31;4mX", "\x1b[2;3;31mX"])
    # An already-upgraded sequence is left alone.
    inputs.extend(["\x1b[38;5;196mX", "\x1b[48;5;21mX", "\x1b[38;2;1;2;3mX", "\x1b[31;38;5;9mX"])
    # An empty parameter list and stray separators.
    inputs.extend(["\x1b[mX", "\x1b[;mX", "\x1b[31;mX", "\x1b[;31mX"])
    # Palette tokens, including the modulo wrap.
    inputs.extend(["{P0}", "{P7}", "{P8}", "{P15}", "{P16}", "{P31}", "{P255}", "{T0}", "{T15}", "{T16}"])
    inputs.extend(["{P1}text{T2}", "\x1b[31m{P2}\x1b[0m"])
    # A sequence that is not SGR is untouched.
    inputs.extend(["\x1b[2J", "\x1b[H", "\x1b[K"])
    return inputs


# A non-default palette proves the palette argument is honoured throughout.
CUSTOM_PALETTE = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]

# Tokens whose digits CPython reads as Unicode decimal digits but ECMAScript
# and Go's RE2 do not. Recorded so the boundary is visible rather than found
# in production.
# The Arabic-Indic digits are the point of these cases, so RUF001's
# ambiguous-character warning is exactly backwards here.
DIALECT_DIVERGENCE_INPUTS = ["{F١٢٣}", "|٠٧", "{P٥}"]  # noqa: RUF001


def main() -> int:
    """Write the golden corpus and report the record count."""
    sections: dict[str, Any] = {
        "generator": "packages/provide-uterm-ts/testdata/gen_ansi_golden.py",
        "constants": {
            "DEFAULT_PALETTE": DEFAULT_PALETTE,
            "DEFAULT_RGB": [list(rgb) for rgb in DEFAULT_RGB],
            "CLEAR_SCREEN": CLEAR_SCREEN,
            "BOLD": BOLD,
            "RESET": RESET,
            "registered_dialects": registered_dialects(),
        },
        "color256_to_rgb": [{"index": i, "rgb": list(_color256_to_rgb(i))} for i in range(256)],
        "extended_tokens": [{"text": t, "out": _handle_extended_tokens(t)} for t in _extended_token_inputs()],
        "tilde_codes": [{"text": t, "out": _handle_tilde_codes(t)} for t in _tilde_inputs()],
        "brace_tokens": [{"text": t, "out": _handle_brace_tokens(t)} for t in _brace_inputs()],
        "pipe_codes": [{"text": t, "out": _handle_pipe_codes(t)} for t in _pipe_inputs()],
        "normalize": [
            {"text": t, "out": normalize_colors(t)}
            for t in _extended_token_inputs() + _tilde_inputs() + _brace_inputs() + _pipe_inputs()
        ],
        "upgrade_256": [{"text": t, "out": upgrade_to_256(t)} for t in _upgrade_inputs()],
        "upgrade_truecolor": [{"text": t, "out": upgrade_to_truecolor(t)} for t in _upgrade_inputs()],
        "upgrade_custom_palette": [
            {
                "text": t,
                "to256": upgrade_to_256(t, CUSTOM_PALETTE),
                "truecolor": upgrade_to_truecolor(t, CUSTOM_PALETTE),
            }
            for t in ["\x1b[31mX", "\x1b[1;31mX", "\x1b[41mX", "{P1}", "{T1}"]
        ],
        "dialect_divergences": [
            {
                "text": t,
                "extended": _handle_extended_tokens(t),
                "pipe": _handle_pipe_codes(t),
            }
            for t in DIALECT_DIVERGENCE_INPUTS
        ],
    }
    OUT.write_text(json.dumps(sections, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    total = sum(len(v) for v in sections.values() if isinstance(v, list))
    print(f"wrote {OUT} ({total} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
