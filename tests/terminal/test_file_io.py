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
