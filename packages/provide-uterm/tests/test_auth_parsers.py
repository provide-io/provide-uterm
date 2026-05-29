#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the option/line parser primitives in ``provide.uterm.auth``.

Covers ``_split_options``, ``_parse_options``, and ``_find_first_token_end``
with edge cases plus surgical mutation-killers (third batch) — empty inputs,
quote-wrapped commas, whitespace handling, partition behaviour, and the
boolean/comparison flips mutmut likes to introduce.
"""

from __future__ import annotations

import pytest

from provide.uterm.auth import (
    _find_first_token_end,
    _parse_options,
    _split_options,
)

# ---------------------------------------------------------------------------
# _split_options
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("options_str", "expected"),
    [
        ("", []),
        ("flag", ["flag"]),
        ("a,b", ["a", "b"]),
        ("a,b,c", ["a", "b", "c"]),
        # Quote-wrapped commas must NOT split.
        ('command="echo a,b"', ['command="echo a,b"']),
        ('a,command="x,y",b', ["a", 'command="x,y"', "b"]),
        # Empty inter-comma segments collapse.
        ("a,,b", ["a", "b"]),
        # Trailing comma drops the empty.
        ("a,", ["a"]),
        # Leading comma drops the empty.
        (",a", ["a"]),
        # Quotes alone produce a single empty-string-with-quotes token.
        ('""', ['""']),
        # Mixed quotes + unquoted commas.
        (
            'no-pty,command="foo",permitopen="1.2.3.4:22"',
            [
                "no-pty",
                'command="foo"',
                'permitopen="1.2.3.4:22"',
            ],
        ),
    ],
)
def test_split_options_known_inputs(options_str: str, expected: list[str]) -> None:
    assert _split_options(options_str) == expected


def test_split_options_preserves_inner_whitespace() -> None:
    # Whitespace inside a token must survive — only commas split.
    assert _split_options("  key = value  ") == ["  key = value  "]


def test_split_options_handles_consecutive_quotes() -> None:
    # Two empty quoted segments separated by a comma — each token keeps its own quotes.
    assert _split_options('"",""') == ['""', '""']


def test_split_options_quotes_open_but_never_close_keeps_everything_as_one() -> None:
    # Unbalanced quote → in_quotes flips and stays True until end of string,
    # so subsequent commas are NOT split points.
    assert _split_options('"a,b,c') == ['"a,b,c']
    assert _split_options('a,"b,c') == ["a", '"b,c']


def test_split_options_buf_drained_only_when_non_empty() -> None:
    # The ``if buf:`` branch protects against emitting an extra empty
    # token at the end. Pin via a trailing-comma case — output has
    # exactly one token, not two.
    assert _split_options("foo,") == ["foo"]
    # And the same for a leading-comma case at the start.
    assert _split_options(",foo") == ["foo"]


def test_split_options_quote_state_persists_across_comma_inside_quotes() -> None:
    # Pin the inner state: while in_quotes is True, commas DON'T split.
    # A mutation that flips the ``not in_quotes`` to ``in_quotes`` would
    # split inside quotes and not split outside.
    assert _split_options('"a,b","c,d"') == ['"a,b"', '"c,d"']


# ---------------------------------------------------------------------------
# _parse_options
# ---------------------------------------------------------------------------


def test_parse_options_empty_returns_empty_dict() -> None:
    assert _parse_options("") == {}


def test_parse_options_boolean_flag_value_is_True_singleton() -> None:  # noqa: N802
    # The flag value must be the bool ``True`` (not the string ``"True"``)
    # because callers branch on it via ``isinstance(value, str)``.
    out = _parse_options("no-pty")
    assert out == {"no-pty": True}
    assert out["no-pty"] is True


def test_parse_options_keyvalue_strips_quotes_and_whitespace() -> None:
    out = _parse_options('command="echo hi"')
    assert out == {"command": "echo hi"}
    # The trimmed value must be a plain str (no surrounding double-quotes left).
    assert out["command"] == "echo hi"


def test_parse_options_keyvalue_strips_whitespace_on_key_and_value() -> None:
    out = _parse_options('  key = "v"  ')
    assert out == {"key": "v"}


def test_parse_options_keyvalue_without_quotes_is_kept_verbatim() -> None:
    # Unquoted values are valid OpenSSH; the parser keeps them as-is after strip.
    out = _parse_options("permitopen=1.2.3.4:22")
    assert out == {"permitopen": "1.2.3.4:22"}


def test_parse_options_multiple_options_combine() -> None:
    out = _parse_options('no-pty,command="x",permitopen="1.2.3.4:22"')
    assert out == {
        "no-pty": True,
        "command": "x",
        "permitopen": "1.2.3.4:22",
    }


def test_parse_options_equal_sign_inside_quoted_value_is_preserved() -> None:
    # ``partition("=")`` consumes only the first ``=``; any later ``=``
    # is part of the value, which must survive after strip+strip('"').
    out = _parse_options('environment="A=B=C"')
    assert out == {"environment": "A=B=C"}


def test_parse_options_keyvalue_with_no_value_yields_empty_string() -> None:
    # ``foo=`` (trailing equals, no value) maps to empty string, not True.
    out = _parse_options("foo=")
    assert out == {"foo": ""}
    assert out["foo"] is not True


def test_parse_options_quotes_with_only_whitespace_inside_are_stripped() -> None:
    # ``strip('"')`` then nothing remains — value is empty string.
    out = _parse_options('key="   "')
    # Whitespace inside quotes is preserved by the parser's partition+strip.
    # Specifically: the value side is `"   "`; .strip() removes the outer
    # whitespace (none here), .strip('"') removes the quotes, leaving
    # `   ` which is NOT further trimmed. Pin that exactly.
    assert out == {"key": "   "}


def test_parse_options_no_options_does_not_invent_a_key() -> None:
    # Defensive: empty input must NOT introduce a `""` key.
    assert "" not in _parse_options("")


def test_parse_options_only_double_quotes_are_stripped_not_single() -> None:
    # ``value.strip('"')`` removes only the literal double-quote character.
    # A value like ``'a'`` (single-quoted) must keep its single quotes —
    # OpenSSH-options grammar doesn't use single quotes for delimiters.
    out = _parse_options("k='a'")
    assert out == {"k": "'a'"}


def test_parse_options_strips_only_double_quotes_not_arbitrary_chars() -> None:
    # ``value.strip('"')`` must strip the literal double-quote character ONLY,
    # not a char set. A value whose content begins/ends with ``X`` (after the
    # quotes are removed) must keep those ``X`` characters intact. Kills the
    # ``strip('"') -> strip('XX"XX')`` mutant, which would also strip ``X``.
    out = _parse_options('k="Xdatax"')
    assert out == {"k": "Xdatax"}


def test_parse_options_partition_consumes_only_the_first_equals_sign() -> None:
    # ``token.partition("=")`` yields (before, "=", after). Any later
    # ``=`` is part of ``after`` and must be preserved verbatim after
    # ``.strip()`` + ``.strip('"')``.
    out = _parse_options('env="A=B=C=D"')
    assert out == {"env": "A=B=C=D"}


def test_parse_options_strips_outer_whitespace_around_key() -> None:
    # ``key.strip()`` is what makes ``  foo=bar`` parse as {"foo": "bar"}
    # — without the strip, the key would be ``"  foo"``.
    out = _parse_options("   foo=bar")
    assert "foo" in out
    assert "  foo" not in out


def test_parse_options_strips_outer_whitespace_around_value() -> None:
    out = _parse_options("foo=   bar   ")
    assert out == {"foo": "bar"}


def test_parse_options_True_is_the_singleton_True_not_string_or_int() -> None:  # noqa: N802
    # Defensive: mutating ``True`` to e.g. ``1`` or ``"True"`` should
    # be caught — flag values must be the singleton bool True.
    out = _parse_options("no-pty")
    assert out["no-pty"] is True
    # bool(1) is also truthy but bool(1) is True only when comparing
    # via `is`. The `is` check above is the pinning assertion.
    assert isinstance(out["no-pty"], bool)


def test_parse_options_value_with_only_double_quotes_collapses_to_empty() -> None:
    # ``""`` value → after .strip('"') the value is empty string, not None,
    # not the literal "" with quotes.
    out = _parse_options('k=""')
    assert out == {"k": ""}
    assert out["k"] == ""


# ---------------------------------------------------------------------------
# _find_first_token_end
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("", 0),
        ("nospace", len("nospace")),
        ("ab cd", 2),
        ("ab\tcd", 2),
        ("ab\ncd", 2),
        # Quotes protect spaces.
        ('command="echo hi" rest', len('command="echo hi"')),
        # Unbalanced quotes still toggle state; whole line is one token.
        ('command="echo', len('command="echo')),
        # Empty quoted value followed by whitespace.
        ('a="" b', len('a=""')),
    ],
)
def test_find_first_token_end(line: str, expected: int) -> None:
    assert _find_first_token_end(line) == expected


def test_find_first_token_end_quotes_toggle_correctly() -> None:
    # Two paired quotes then a space — the space terminates.
    line = '"x""y" rest'
    # Index of the space after the second closing quote.
    assert _find_first_token_end(line) == line.index(" ")


def test_find_first_token_end_returns_length_when_no_whitespace_outside_quotes() -> None:
    # The else branch (``return len(line)``) fires when the loop completes
    # without finding an unquoted whitespace. Pin both halves.
    assert _find_first_token_end("contiguous") == len("contiguous")
    assert _find_first_token_end('a"b c"d') == len('a"b c"d')


def test_find_first_token_end_returns_zero_for_leading_whitespace() -> None:
    # Leading space at position 0 must return 0 (not 1 or len).
    assert _find_first_token_end(" rest") == 0
    assert _find_first_token_end("\trest") == 0
    assert _find_first_token_end("\nrest") == 0
