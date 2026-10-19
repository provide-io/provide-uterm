#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the Unicode and charset tables the Go, C# and TypeScript screens draw with.

The Python reference draws with pyte, which measures a character with wcwidth,
composes a combining mark with unicodedata (pinned to unicodedata2, see
provide/uterm/_unicode.py) and translates through pyte's charset maps. The
ports cannot import those, so this writes their data into each port, from the
exact versions uv.lock pins:

- the width of every code point whose wcwidth is not 1;
- the canonical combining class of every code point that has one;
- the canonical decompositions and primary compositions NFC uses;
- pyte's Latin-1, VT100, IBM PC and VAX42 charset maps (Go and C#; the
  TypeScript screen keeps only the two it translates by table).

Run with no arguments to rewrite the tables, or ``--check`` to fail when a
committed table no longer matches (CI does, from ci/quality_checks.sh). Each
port also tests its tables against the vt golden corpus's export of the same
data, so a wcwidth or unicodedata2 upgrade fails there until this is rerun.

Usage (from the repository root)::

    uv run python scripts/gen_unicode_tables.py [--check]
"""

from __future__ import annotations

import argparse
import functools
import sys
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING

import unicodedata2
import wcwidth
from pyte import charsets

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
GO_VT = REPO_ROOT / "packages" / "provide-uterm-go" / "vt"
CS_VT = REPO_ROOT / "packages" / "provide-uterm-csharp" / "src" / "Provide.Uterm" / "Vt"
TS_VT = REPO_ROOT / "packages" / "provide-uterm-ts" / "src" / "vt"

MAX_CODE_POINT = 0x10FFFF
# The precomposed Hangul syllables: every port decomposes and composes them by
# arithmetic (Unicode 3.12), and unicodedata2 reports their decompositions too.
HANGUL_FIRST, HANGUL_LAST = 0xAC00, 0xD7A3
# Entries to a line in the Go and C# normalization tables: five keeps both
# files inside the 777-line limit (scripts/check_max_loc.py).
NORM_PER_LINE = 5

# REUSE-IgnoreStart -- the header below is emitted INTO generated files.
LICENSE_LINES = (
    "SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.",
    "SPDX-License-Identifier: AGPL-3.0-or-later",
)
# REUSE-IgnoreEnd

WIDTHS = f"wcwidth {version('wcwidth')} (Unicode {wcwidth.list_versions()[-1]} widths)"
UNICODE = f"unicodedata2 {version('unicodedata2')} (Unicode {unicodedata2.unidata_version})"
CHARSETS = f"pyte {version('pyte')}"
SOURCE = f"{WIDTHS}, {UNICODE} and {CHARSETS}"


# --- The data ----------------------------------------------------------------


def runs(value: Callable[[int], int], skip: int) -> list[tuple[int, int, int]]:
    """Every run of consecutive code points with one value, other than ``skip``."""
    out: list[tuple[int, int, int]] = []
    for point in range(MAX_CODE_POINT + 1):
        v = value(point)
        if v == skip:
            continue
        if out and out[-1][1] == point - 1 and out[-1][2] == v:
            out[-1] = (out[-1][0], point, v)
        else:
            out.append((point, point, v))
    return out


@functools.cache
def width_runs() -> list[tuple[int, int, int]]:
    """The runs whose wcwidth is not 1."""
    return runs(lambda point: wcwidth.wcwidth(chr(point)), 1)


@functools.cache
def combining_runs() -> list[tuple[int, int, int]]:
    """The runs with a non-zero canonical combining class."""
    return runs(lambda point: unicodedata2.combining(chr(point)), 0)


@functools.cache
def decompositions() -> list[tuple[int, int, int]]:
    """Each canonical one-level decomposition as (code point, first, second or -1).

    Hangul syllables decompose algorithmically and have no entry.
    """
    out = []
    for point in range(MAX_CODE_POINT + 1):
        if HANGUL_FIRST <= point <= HANGUL_LAST:
            continue
        mapping = unicodedata2.decomposition(chr(point))
        if not mapping or mapping.startswith("<"):
            continue
        parts = [int(part, 16) for part in mapping.split()]
        out.append((point, parts[0], parts[1] if len(parts) > 1 else -1))
    return out


def compositions() -> list[tuple[int, int, int]]:
    """Each primary composite as (starter, combiner, composite), in starter order.

    A two-part canonical decomposition is a primary composite exactly when NFC
    recomposes it, which leaves out the composition exclusions.
    """
    pairs = [
        (first, second, point)
        for point, first, second in decompositions()
        if second >= 0 and unicodedata2.normalize("NFC", chr(first) + chr(second)) == chr(point)
    ]
    return sorted(pairs)


def charset_map(table: str) -> list[int]:
    """A pyte charset map as its 256 code points."""
    return [ord(char) for char in table]


# --- Formatting ----------------------------------------------------------------


def rows(items: Iterable[str], per_line: int, indent: str) -> str:
    """Join items ``per_line`` to a line, each line indented and ending in a comma."""
    items = list(items)
    lines = [", ".join(items[i : i + per_line]) for i in range(0, len(items), per_line)]
    return "".join(f"{indent}{line},\n" for line in lines)


def h(value: int) -> str:
    """A code point as the tables write it: four or more upper-case hex digits."""
    return f"0x{value:04X}" if value >= 0 else str(value)


def slash_header(source: str, *extra: str) -> str:
    generated = f"Code generated by scripts/gen_unicode_tables.py from {source}. DO NOT EDIT."
    lines = ["//", *(f"// {line}" for line in LICENSE_LINES), "//", "", f"// {generated}", *extra]
    return "\n".join(lines) + "\n"


def go_width() -> str:
    return (
        slash_header(f"{WIDTHS} and {UNICODE}", "", "package vt", "")
        + "// wcwidthRanges lists codepoint ranges whose wcwidth differs from 1.\n"
        + "var wcwidthRanges = []widthRange{\n"
        + rows((f"{{{h(a)}, {h(b)}, {v}}}" for a, b, v in width_runs()), 4, "\t")
        + "}\n\n// combiningRanges lists codepoint ranges with nonzero canonical combining class.\n"
        + "var combiningRanges = []widthRange{\n"
        + rows((f"{{{h(a)}, {h(b)}, {v}}}" for a, b, v in combining_runs()), 4, "\t")
        + "}\n"
    )


def go_norm() -> str:
    return (
        slash_header(UNICODE, "", "package vt", "")
        + "// canonicalDecomp maps codepoints to their one-level canonical decomposition.\n"
        + "var canonicalDecomp = map[rune][2]rune{\n"
        + rows((f"{h(p)}: {{{h(a)}, {h(b)}}}" for p, a, b in decompositions()), NORM_PER_LINE, "\t")
        + "}\n\n// compositionPairs maps (starter, combiner) to the primary composite.\n"
        + "var compositionPairs = map[compKey]rune{\n"
        + rows((f"{{{h(a)}, {h(b)}}}: {h(c)}" for a, b, c in compositions()), NORM_PER_LINE, "\t")
        + "}\n"
    )


GO_MAPS = (
    ("lat1Map", "the Latin-1 (identity) charset map", charsets.LAT1_MAP),
    ("vt100Map", "the VT100 special graphics charset map", charsets.VT100_MAP),
    ("ibmpcMap", "the IBM codepage 437 charset map", charsets.IBMPC_MAP),
    ("vax42Map", "the VAX42 charset map", charsets.VAX42_MAP),
)


def go_charsets() -> str:
    body = "\n".join(
        f"// {name} is {doc}.\nvar {name} = charsetMap{{\n" + rows(map(h, charset_map(table)), 8, "\t") + "}\n"
        for name, doc, table in GO_MAPS
    )
    return slash_header(CHARSETS, "", "package vt", "") + body


def cs_class(source: str, name: str, fields: str) -> str:
    return (
        slash_header(source, "", "namespace Provide.Uterm.Vt;", "", f"internal static class {name}", "{")
        + fields
        + "}\n"
    )


def cs_width() -> str:
    def table(name: str, data: list[tuple[int, int, int]]) -> str:
        entries = rows((f"new({h(a)}, {h(b)}, {v})" for a, b, v in data), 4, "        ")
        return f"    internal static readonly WidthRange[] {name} =\n    [\n{entries}    ];\n"

    return cs_class(
        f"{WIDTHS} and {UNICODE}",
        "UnicodeWidthTables",
        table("WcwidthRanges", width_runs()) + "\n" + table("CombiningRanges", combining_runs()),
    )


def cs_norm() -> str:
    decomp = rows((f"{{ {h(p)}, ({h(a)}, {h(b)}) }}" for p, a, b in decompositions()), NORM_PER_LINE, "        ")
    comp = rows((f"{{ ({h(a)}, {h(b)}), {h(c)} }}" for a, b, c in compositions()), NORM_PER_LINE, "        ")
    return cs_class(
        UNICODE,
        "UnicodeNormTables",
        "    // A second code point of -1 marks a singleton: one code point decomposing to one.\n"
        "    internal static readonly Dictionary<int, (int, int)> CanonicalDecomp = new()\n"
        f"    {{\n{decomp}    }};\n\n"
        "    internal static readonly Dictionary<(int, int), int> CompositionPairs = new()\n"
        f"    {{\n{comp}    }};\n",
    )


CS_MAPS = (
    ("Lat1Map", charsets.LAT1_MAP),
    ("Vt100Map", charsets.VT100_MAP),
    ("IbmpcMap", charsets.IBMPC_MAP),
    ("Vax42Map", charsets.VAX42_MAP),
)


def cs_charsets() -> str:
    # One entry to a line, as the file has always been: it is on the max-LOC
    # ratchet (.ci/max-loc-baseline.json) at this size.
    def table(name: str, values: list[int]) -> str:
        entries = ",\n".join(f"        {h(v)}" for v in values)
        return f"    internal static readonly int[] {name} =\n    [\n{entries}\n    ];\n"

    return cs_class(CHARSETS, "CharsetTables", "\n".join(table(name, charset_map(t)) for name, t in CS_MAPS))


def ts_const(doc: str, name: str, values: Iterable[str], per_line: int) -> str:
    # biome-ignore keeps the layout this writes, so --check compares like with like.
    return (
        f"/** {doc} */\n// biome-ignore format: generated table\nexport const {name}: readonly number[] = [\n"
        + rows(values, per_line, "  ")
        + "];\n"
    )


def ts_hex(value: int) -> str:
    return f"0x{value:x}" if value >= 0 else str(value)


def ts_module(source: str, doc: str, *consts: str) -> str:
    return slash_header(source, "", "/**", *(f" * {line}" if line else " *" for line in doc.split("\n")), " */", "") + (
        "\n".join(consts)
    )


def ts_width() -> str:
    return ts_module(
        f"{WIDTHS} and {UNICODE}",
        "The cell width the reference gives each code point, and its canonical\ncombining class. See ./width.ts.",
        ts_const(
            "Every run of code points whose width is not 1, as flat `first, last, width` triples.",
            "WIDTH_RANGES",
            (f"{ts_hex(a)}, {ts_hex(b)}, {v}" for a, b, v in width_runs()),
            6,
        ),
        ts_const(
            "Every run of code points with a non-zero combining class, as flat `first, last, class` triples.",
            "COMBINING_CLASSES",
            (f"{ts_hex(a)}, {ts_hex(b)}, {v}" for a, b, v in combining_runs()),
            6,
        ),
    )


def ts_norm() -> str:
    return ts_module(
        UNICODE,
        "The canonical decompositions and primary compositions NFC uses, the\nreference's. See ./normalize.ts.",
        ts_const(
            "Each canonical one-level decomposition as flat `code point, first, second` triples;"
            " a second of -1 is a singleton.",
            "CANONICAL_DECOMPOSITIONS",
            (f"{ts_hex(p)}, {ts_hex(a)}, {ts_hex(b)}" for p, a, b in decompositions()),
            5,
        ),
        ts_const(
            "Each primary composite as flat `starter, combiner, composite` triples.",
            "COMPOSITION_PAIRS",
            (f"{ts_hex(a)}, {ts_hex(b)}, {ts_hex(c)}" for a, b, c in compositions()),
            5,
        ),
    )


def ts_charsets() -> str:
    return ts_module(
        CHARSETS,
        "The 256-entry translation tables for the sets `ESC ( U` and `ESC ( V`\ndesignate. See ./charsets.ts.",
        ts_const(
            "IBM codepage 437: the PC's glyphs for the C0 range and DEL, and its letters, box drawing and"
            " symbols above 0x7F.",
            "IBMPC_MAP",
            map(ts_hex, charset_map(charsets.IBMPC_MAP)),
            8,
        ),
        ts_const("The VAX42 set.", "VAX42_MAP", map(ts_hex, charset_map(charsets.VAX42_MAP)), 8),
    )


OUTPUTS: dict[Path, Callable[[], str]] = {
    GO_VT / "unicode_width_tables.go": go_width,
    GO_VT / "unicode_norm_tables.go": go_norm,
    GO_VT / "charset_tables.go": go_charsets,
    CS_VT / "UnicodeWidthTables.cs": cs_width,
    CS_VT / "UnicodeNormTables.cs": cs_norm,
    CS_VT / "CharsetTables.cs": cs_charsets,
    TS_VT / "wcwidth-tables.ts": ts_width,
    TS_VT / "normalization-tables.ts": ts_norm,
    TS_VT / "charset-tables.ts": ts_charsets,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="fail if a committed table is out of date")
    args = parser.parse_args(argv)
    stale = []
    for path, render in OUTPUTS.items():
        content = render()
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(path.relative_to(REPO_ROOT))
        else:
            path.write_text(content, encoding="utf-8")
    if stale:
        for path in stale:
            print(f"stale: {path} (regenerate with: uv run python scripts/gen_unicode_tables.py)", file=sys.stderr)
        return 1
    print(f"{'checked' if args.check else 'wrote'} {len(OUTPUTS)} tables from {SOURCE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
