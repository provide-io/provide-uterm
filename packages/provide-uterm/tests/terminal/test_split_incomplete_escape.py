#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""``split_incomplete_escape`` carries exactly what ``strip_ansi`` would strip once complete.

Both are built from the same grammar constants in ``screen.py``. The property
pinned here: for every sequence strip_ansi strips (no longer than
``MAX_ESCAPE_CARRY``), cut at every position, splitting the first part and
carrying its tail into the second strips to exactly what stripping the whole
text does -- and a cut strictly inside the sequence carries all of its head.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from provide.uterm import MAX_ESCAPE_CARRY, split_incomplete_escape, strip_ansi
from provide.uterm.screen import _ANSI_ESCAPE_RE

_PREFIX = "ab"
# Neither a CSI parameter, intermediate nor final byte, so the sequence under
# test ends where it is meant to and nothing after it is pulled into it.
_SUFFIX = "\n!"


def _fe_sequences() -> list[str]:
    return [f"\x1b{chr(c)}" for c in range(0x40, 0x60)]


def _csi_sequences() -> list[str]:
    params = ["", "1", "38;2;255;0;0", "?25", "<=>", "0;1:2"]
    intermediates = ["", " ", "!", "/ "]
    finals = [chr(c) for c in range(0x40, 0x7F)]
    return [f"\x1b[{p}{i}{f}" for p in params for i in intermediates for f in finals]


_SEQUENCES = _fe_sequences() + _csi_sequences()


def _assert_carried_correctly(seq: str) -> None:
    assert _ANSI_ESCAPE_RE.fullmatch(seq), seq  # a sequence strip_ansi strips
    whole = _PREFIX + seq + _SUFFIX
    expected = strip_ansi(whole)
    assert expected == _PREFIX + _SUFFIX
    for cut in range(len(seq) + 1):
        complete, carry = split_incomplete_escape(_PREFIX + seq[:cut])
        assert complete + carry == _PREFIX + seq[:cut]
        if 0 < cut < len(seq):
            assert carry == seq[:cut], (seq, cut)
        if cut == 0:
            assert carry == ""
        assert strip_ansi(complete) + strip_ansi(carry + seq[cut:] + _SUFFIX) == expected, (seq, cut)


def test_every_enumerated_sequence_is_carried_correctly_at_every_cut() -> None:
    assert len(_SEQUENCES) == 32 + 6 * 4 * 63
    for seq in _SEQUENCES:
        _assert_carried_correctly(seq)


@given(st.from_regex(_ANSI_ESCAPE_RE, fullmatch=True).filter(lambda s: len(s) <= MAX_ESCAPE_CARRY))
def test_any_sequence_strip_ansi_strips_is_carried_correctly_at_every_cut(seq: str) -> None:
    _assert_carried_correctly(seq)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ("", "")),
        ("plain", ("plain", "")),
        ("a\x1b", ("a", "\x1b")),
        ("a\x1b[38;2;255", ("a", "\x1b[38;2;255")),
        ("a\x1b[1mb", ("a\x1b[1mb", "")),
        ("a\x1bc", ("a\x1bc", "")),
        ("\x1b[1m\x1b[3", ("\x1b[1m", "\x1b[3")),
        ("\x1b[" + "1" * (MAX_ESCAPE_CARRY - 2), ("", "\x1b[" + "1" * (MAX_ESCAPE_CARRY - 2))),
        ("\x1b[" + "1" * (MAX_ESCAPE_CARRY - 1), ("\x1b[" + "1" * (MAX_ESCAPE_CARRY - 1), "")),
    ],
)
def test_split_incomplete_escape_examples(text: str, expected: tuple[str, str]) -> None:
    assert split_incomplete_escape(text) == expected


def test_max_carry_is_configurable_and_defaults_to_64() -> None:
    assert MAX_ESCAPE_CARRY == 64
    assert split_incomplete_escape("x\x1b[12", max_carry=4) == ("x", "\x1b[12")
    assert split_incomplete_escape("x\x1b[123", max_carry=4) == ("x\x1b[123", "")
