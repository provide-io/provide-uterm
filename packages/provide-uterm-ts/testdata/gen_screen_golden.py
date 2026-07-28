#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript ``screen`` port.

Node has no built-in CP437 codec, so the full 256-entry table is exported
here rather than hand-transcribed: the reference codec is the provenance.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_screen_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.screen import (
    clean_screen_for_display,
    decode_cp437,
    encode_cp437,
    extract_action_tags,
    extract_key_value_pairs,
    extract_menu_options,
    extract_numbered_list,
    normalize_terminal_text,
)

OUT = Path(__file__).with_name("screen_golden.json")


def _normalize_inputs() -> list[str]:
    """Text for the ANSI-stripping normaliser."""
    return [
        "",
        "plain text",
        # Line-ending normalisation.
        "a\r\nb",
        "a\rb",
        "a\nb",
        "a\r\n\r\nb",
        "\r\n",
        # CSI sequences of every shape the pattern allows.
        "\x1b[31mred\x1b[0m",
        "\x1b[1;31;42mx",
        "\x1b[2J\x1b[H",
        "\x1b[?25l\x1b[?25h",
        "\x1b[38;5;196mx",
        "\x1b[K",
        "\x1b[10;20H",
        # Two-character escapes.
        "\x1bMx",
        "\x1b7\x1b8",
        "\x1b=x",
        # An escape with no final byte survives, because the pattern needs one.
        "\x1b[31",
        "\x1b",
        # Bare SGR fragments leaked by BBS servers.
        "1;31mHELLO",
        "31mHELLO",
        "1;31m<Move>",
        "text 1;31m more",
        "a\n1;31mB",
        "a\r\n31m<Tag>",
        # A bare fragment that is not isolated must survive.
        "abc1;31mdef",
        "1;31mlowercase",
        # Digits too long for the fragment pattern.
        "1234mX",
        # Mixed real and bare sequences.
        "\x1b[31m1;32mtext",
        # Non-ASCII passes through.
        "café \x1b[31mx",
    ]


def _action_tag_inputs() -> list[tuple[str, int]]:
    """(text, max_tags) for the action-tag extractor."""
    return [
        ("", 8),
        ("no tags", 8),
        ("<Move>", 8),
        ("<Move> <Attack>", 8),
        # De-duplication is case-insensitive but keeps the first spelling.
        ("<Move> <move> <MOVE>", 8),
        # Surrounding whitespace is trimmed.
        ("<  Move  >", 8),
        # An empty or whitespace-only tag is skipped.
        ("<> <Move>", 8),
        ("<   > <Move>", 8),
        # The cap is honoured, and a cap below one is raised to one.
        ("<a> <b> <c> <d>", 2),
        ("<a> <b>", 0),
        ("<a> <b>", -5),
        # Tags cannot span a line break or contain angle brackets.
        ("<a\nb>", 8),
        ("<a<b>", 8),
        # A tag longer than the eighty-character bound is not a tag.
        (f"<{'x' * 80}>", 8),
        (f"<{'x' * 81}>", 8),
        ("prefix <Tag> suffix", 8),
    ]


def _clean_screen_inputs() -> list[tuple[str, int]]:
    """(screen, max_lines) for the display cleaner."""
    return [
        ("", 30),
        ("one\ntwo\nthree", 30),
        ("one\n\ntwo", 30),
        # A line of exactly eighty spaces is padding and is dropped.
        (" " * 80, 30),
        (" " * 80 + "\ncontent", 30),
        (" " * 79, 30),
        (" " * 81, 30),
        (" " * 80 + "x", 30),
        # The cap stops the scan.
        ("a\nb\nc\nd", 2),
        ("a\nb", 0),
    ]


def _menu_inputs() -> list[tuple[str, str | None]]:
    """(screen, pattern) for the menu extractor."""
    return [
        ("", None),
        ("<A> Move", None),
        ("[B] Attack", None),
        ("(C) Flee", None),
        ("<A> Move <B> Attack", None),
        ("<A> Move\n<B> Attack", None),
        # A lowercase key is not matched by the default pattern.
        ("<a> move", None),
        # An empty description is dropped.
        ("<A>   ", None),
        # A custom pattern with two groups.
        ("A=Move B=Attack", r"([A-Z])=(\w+)"),
        # An invalid custom pattern yields nothing rather than raising.
        ("<A> Move", "([A-Z]"),
    ]


def _numbered_inputs() -> list[tuple[str, str | None]]:
    """(screen, pattern) for the numbered-list extractor."""
    return [
        ("", None),
        ("1. First", None),
        ("2) Second", None),
        ("  3. Indented", None),
        ("1. First\n2. Second", None),
        # A dash separator is not matched by the default pattern.
        ("1 - Dashed", None),
        # A trailing-whitespace description is trimmed, and an empty one dropped.
        ("1.    ", None),
        ("1. Spaced   ", None),
        ("not a list", None),
        ("1. First", r"(\d+)\.\s+(.+)"),
        ("1. First", "(\\d+"),
    ]


def _key_value_inputs() -> list[tuple[str, dict[str, str]]]:
    """(screen, patterns) for the key-value extractor."""
    return [
        ("Credits: 1,234  Sector: 42", {"credits": r"Credits?:?\s*([\d,]+)", "sector": r"Sector\s*:?\s*(\d+)"}),
        # Matching is case-insensitive.
        ("credits: 99", {"credits": r"Credits:\s*(\d+)"}),
        # A field that does not match is simply absent.
        ("nothing here", {"credits": r"Credits:\s*(\d+)"}),
        # An invalid pattern is skipped rather than raising.
        ("Credits: 5", {"bad": "([0-9]", "credits": r"Credits:\s*(\d+)"}),
        ("", {"credits": r"Credits:\s*(\d+)"}),
    ]


def main() -> int:
    """Write the golden corpus and report the record count."""
    # The full CP437 table: byte value to the code point it decodes to.
    cp437_table = [ord(decode_cp437(bytes([b]))) for b in range(256)]

    payload: dict[str, Any] = {
        "generator": "packages/provide-uterm-ts/testdata/gen_screen_golden.py",
        "cp437_table": cp437_table,
        "cp437_encode": [
            {"text": t, "bytes": encode_cp437(t).hex()} for t in ["", "abc", "░▒▓", "Éé", "你好", "a你b", "─│┌"]
        ],
        "normalize": [{"text": t, "out": normalize_terminal_text(t)} for t in _normalize_inputs()],
        "action_tags": [
            {"text": t, "max_tags": m, "out": extract_action_tags(t, max_tags=m)} for (t, m) in _action_tag_inputs()
        ],
        "clean_screen": [
            {"screen": s, "max_lines": m, "out": clean_screen_for_display(s, m)} for (s, m) in _clean_screen_inputs()
        ],
        "menu_options": [
            {"screen": s, "pattern": p, "out": [list(o) for o in extract_menu_options(s, p)]}
            for (s, p) in _menu_inputs()
        ],
        "numbered_list": [
            {"screen": s, "pattern": p, "out": [list(o) for o in extract_numbered_list(s, p)]}
            for (s, p) in _numbered_inputs()
        ],
        "key_values": [
            {"screen": s, "patterns": p, "out": extract_key_value_pairs(s, p)} for (s, p) in _key_value_inputs()
        ],
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    total = sum(len(v) for v in payload.values() if isinstance(v, list))
    print(f"wrote {OUT} ({total} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
