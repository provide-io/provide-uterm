#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The Unicode data every port draws with is the data the reference draws with.

The vt corpus (packages/provide-uterm-ts/testdata/gen_vt_golden.py) exports the
wcwidth of every code point, the combining classes and the NFC tables, and the
Go, C# and TypeScript ports check their generated tables
(scripts/gen_unicode_tables.py) against that export. Both come from the
versions uv.lock pins: wcwidth, and unicodedata2 rather than the
interpreter's unicodedata, which moves from 14.0 on 3.11 to 16.0 on 3.14. So
every comparison here is the same on every interpreter. It fails when a pinned
version moves without the corpus and the ports' tables being regenerated.
"""

import json
from pathlib import Path
from typing import Any

import pyte.screens
import unicodedata2
from wcwidth import wcwidth

from provide.uterm import _unicode
from provide.uterm.emulator import TerminalEmulator

_CORPUS: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[4] / "packages" / "provide-uterm-ts" / "testdata" / "vt_golden.json").read_text(
        encoding="utf-8"
    )
)


def _expand(ranges: list[list[int]]) -> dict[int, int]:
    return {point: entry[2] if len(entry) > 2 else 1 for entry in ranges for point in range(entry[0], entry[1] + 1)}


def test_the_exported_widths_are_the_pinned_wcwidth() -> None:
    widths = _expand(_CORPUS["wcwidth_ranges"])

    assert [point for point in range(0x110000) if wcwidth(chr(point)) != widths.get(point, 1)] == []


def test_the_exported_combining_classes_are_the_pinned_unicode() -> None:
    classes = _expand(_CORPUS["combining_classes"])

    assert _CORPUS["unicode_version"] == unicodedata2.unidata_version == _unicode.UNICODE_VERSION
    assert [point for point in range(0x110000) if unicodedata2.combining(chr(point)) != classes.get(point, 0)] == []
    assert _expand(_CORPUS["combining_ranges"]).keys() == classes.keys()


def test_the_exported_compositions_are_what_the_pinned_nfc_composes() -> None:
    for first, second, composite in _CORPUS["composition_pairs"]:
        assert unicodedata2.normalize("NFC", chr(first) + chr(second)) == chr(composite)
    decomposed = {point: (first, second) for point, first, second in _CORPUS["canonical_decompositions"]}
    for point, (first, second) in decomposed.items():
        expected = " ".join(f"{part:04X}" for part in (first, second) if part >= 0)
        assert unicodedata2.decomposition(chr(point)) == expected


def test_pyte_composes_with_the_pinned_unicode_not_the_interpreters() -> None:
    # U+105C9 is the Unicode 16 composite of this pair: the pinned data
    # composes it on every interpreter, where CPython's own unicodedata does
    # only from 3.14.
    emulator = TerminalEmulator(4, 1, receive_encoding="utf-8")
    emulator.process("\U000105d2\u0307".encode())

    assert pyte.screens.unicodedata is unicodedata2
    assert emulator._screen.buffer[0][0].data == "\U000105c9"
