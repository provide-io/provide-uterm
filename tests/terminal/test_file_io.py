#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.file_io."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from provide.terminal.ansi import DEFAULT_PALETTE
from provide.terminal.file_io import load_ans, load_palette, load_txt

if TYPE_CHECKING:
    from pathlib import Path


def test_load_ans_latin1(tmp_path: Path) -> None:
    raw = b"Hello \xff world"
    f = tmp_path / "test.ans"
    f.write_bytes(raw)
    result = load_ans(f)
    assert result == raw.decode("latin-1")


def test_load_ans_accepts_str_path(tmp_path: Path) -> None:
    raw = b"\x1b[31mRed\x1b[0m"
    f = tmp_path / "art.ans"
    f.write_bytes(raw)
    result = load_ans(str(f))
    assert result == raw.decode("latin-1")


def test_load_txt_utf8(tmp_path: Path) -> None:
    content = "Hello, world! \u00e9"
    f = tmp_path / "test.txt"
    f.write_text(content, encoding="utf-8")
    assert load_txt(f) == content


def test_load_txt_accepts_str_path(tmp_path: Path) -> None:
    content = "plain text"
    f = tmp_path / "test.txt"
    f.write_text(content, encoding="utf-8")
    assert load_txt(str(f)) == content


def test_load_palette_none() -> None:
    result = load_palette(None)
    assert result == DEFAULT_PALETTE
    # Should be a copy, not the same object
    result.append(999)
    assert len(DEFAULT_PALETTE) == 16


def test_load_palette_valid_json(tmp_path: Path) -> None:
    pal = list(range(16))
    f = tmp_path / "palette.json"
    f.write_text(json.dumps(pal), encoding="utf-8")
    assert load_palette(f) == pal


def test_load_palette_accepts_str_path(tmp_path: Path) -> None:
    pal = [0] * 16
    f = tmp_path / "palette.json"
    f.write_text(json.dumps(pal), encoding="utf-8")
    assert load_palette(str(f)) == pal


def test_load_palette_wrong_length(tmp_path: Path) -> None:
    f = tmp_path / "bad.json"
    f.write_text(json.dumps([0, 1, 2]), encoding="utf-8")
    with pytest.raises(ValueError, match="16"):
        load_palette(f)


def test_load_palette_out_of_range(tmp_path: Path) -> None:
    pal = [0] * 15 + [256]
    f = tmp_path / "bad.json"
    f.write_text(json.dumps(pal), encoding="utf-8")
    with pytest.raises(ValueError, match="0..255"):
        load_palette(f)


def test_load_palette_negative_value(tmp_path: Path) -> None:
    pal = [-1] + [0] * 15
    f = tmp_path / "bad.json"
    f.write_text(json.dumps(pal), encoding="utf-8")
    with pytest.raises(ValueError):
        load_palette(f)


# ---------------------------------------------------------------------------
# Mutation-killing tests for load_palette
# ---------------------------------------------------------------------------


def test_load_palette_value_255_is_valid(tmp_path: Path) -> None:
    """Value 255 must be accepted as a valid palette entry.

    Kills mutmut_22: 0 <= v <= 255 → 0 <= v < 255.
    With mutation, v=255 raises ValueError; with original it is accepted.
    """
    pal = [0] * 15 + [255]
    f = tmp_path / "palette.json"
    f.write_text(json.dumps(pal), encoding="utf-8")
    result = load_palette(f)
    assert result[-1] == 255, "Value 255 must be accepted as valid (0 <= v <= 255)"


def test_load_palette_wrong_length_error_message(tmp_path: Path) -> None:
    """ValueError for wrong-length palette must mention the exact wording.

    Kills mutmut_13 (XXpalette mapXX), mutmut_14 (lowercase 'json'),
    and mutmut_15 (all-uppercase).
    """
    f = tmp_path / "bad.json"
    f.write_text(json.dumps([0, 1, 2]), encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        load_palette(f)
    msg = str(exc_info.value)
    assert "palette map" in msg, f"Error must say 'palette map', got: {msg!r}"
    assert "JSON" in msg, f"Error must say 'JSON' (uppercase), got: {msg!r}"
    assert "16" in msg, f"Error must mention '16', got: {msg!r}"
    assert "integers" in msg, f"Error must say 'integers', got: {msg!r}"


def test_load_palette_out_of_range_error_message(tmp_path: Path) -> None:
    """ValueError for out-of-range value must mention exact wording.

    Kills mutmut_25 (XXpalette map valuesXX) and mutmut_26 (all-uppercase).
    """
    pal = [0] * 15 + [256]
    f = tmp_path / "bad.json"
    f.write_text(json.dumps(pal), encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        load_palette(f)
    msg = str(exc_info.value)
    assert "palette map values" in msg, f"Error must say 'palette map values', got: {msg!r}"
    assert "0..255" in msg, f"Error must mention '0..255', got: {msg!r}"


def test_load_txt_default_encoding_reads_utf8(tmp_path: Path) -> None:
    """load_txt uses utf-8 by default — verifies the default encoding parameter.

    Kills mutmut_3: encoding=encoding → encoding=None.
    With encoding=None, read_text uses locale default (may not be UTF-8).
    By writing non-ASCII UTF-8 content and reading it back, we confirm UTF-8 is used.
    """
    content = "café résumé naïve"
    f = tmp_path / "utf8.txt"
    f.write_text(content, encoding="utf-8")
    result = load_txt(f)
    assert result == content, f"load_txt default must use utf-8, got {result!r}"


def test_load_ans_default_encoding_reads_latin1(tmp_path: Path) -> None:
    """load_ans uses latin-1 by default — verifies the default encoding parameter.

    Kills mutmut_1 (XXlatin-1XX would raise LookupError) and
    mutmut_2 (LATIN-1 is equivalent to latin-1 — this test is to document
    that the default works for latin-1 encoded bytes).
    """
    # 0xFF is valid latin-1 (ÿ) but would be different in other encodings
    raw = b"BBS art \xff test"
    f = tmp_path / "test.ans"
    f.write_bytes(raw)
    result = load_ans(f)
    assert result == raw.decode("latin-1"), "load_ans default must decode as latin-1"
