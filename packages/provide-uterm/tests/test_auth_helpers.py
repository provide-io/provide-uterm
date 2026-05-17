#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Exhaustive tests for ``provide.uterm.auth`` internal helpers.

These tests target the private option/line parsers (``_split_options``,
``_parse_options``, ``_find_first_token_end``, ``_parse_authorized_keys_line``)
with edge cases that the integration-style ``TestAuthorizedKeysFileResolver``
suite did not exercise — empty inputs, single/multiple tokens, quote-wrapped
commas, whitespace handling, and boundary conditions on ``len()`` checks.

Goal: bring the mutmut kill rate on ``auth.py`` from 70.65% toward 100% by
pinning the exact behavior of every parser branch the mutation gate touches.
"""

from __future__ import annotations

import pytest

from provide.uterm.auth import (
    _find_first_token_end,
    _parse_authorized_keys_line,
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
        ('no-pty,command="foo",permitopen="1.2.3.4:22"', [
            "no-pty",
            'command="foo"',
            'permitopen="1.2.3.4:22"',
        ]),
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


# ---------------------------------------------------------------------------
# _parse_options
# ---------------------------------------------------------------------------


def test_parse_options_empty_returns_empty_dict() -> None:
    assert _parse_options("") == {}


def test_parse_options_boolean_flag_value_is_True_singleton() -> None:
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
    assert out["foo"] is not True  # noqa: E712


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


# ---------------------------------------------------------------------------
# _parse_authorized_keys_line
# ---------------------------------------------------------------------------


_ED25519_SAMPLE = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK7nKaxTKmzX0z3V6tGqmmOvkSiGXh3yF2J5vqkQTOY+"
)


def test_parse_authorized_keys_line_no_options_comment_becomes_subject() -> None:
    entry = _parse_authorized_keys_line(f"{_ED25519_SAMPLE} alice@laptop")
    assert entry.subject == "alice@laptop"
    assert entry.claims == {}
    assert entry.fingerprint.startswith("SHA256:")


def test_parse_authorized_keys_line_no_options_no_comment_falls_back_to_fingerprint() -> None:
    entry = _parse_authorized_keys_line(_ED25519_SAMPLE)
    assert entry.subject.startswith("key:SHA256:")
    assert entry.claims == {}


def test_parse_authorized_keys_line_subject_option_wins_over_comment() -> None:
    line = f'subject="bob",no-pty {_ED25519_SAMPLE} alice@laptop'
    entry = _parse_authorized_keys_line(line)
    assert entry.subject == "bob"
    # no-pty is a flag option — kept under _options.
    assert "_options" in entry.claims
    assert entry.claims["_options"] == {"no-pty": True}


def test_parse_authorized_keys_line_claim_prefix_options_become_claims() -> None:
    line = f'claim-team="ops",claim-role="oncall" {_ED25519_SAMPLE} c'
    entry = _parse_authorized_keys_line(line)
    assert entry.claims == {"team": "ops", "role": "oncall"}


def test_parse_authorized_keys_line_mixed_claim_and_non_claim_options() -> None:
    line = f'claim-team="ops",no-pty {_ED25519_SAMPLE} c'
    entry = _parse_authorized_keys_line(line)
    assert entry.claims["team"] == "ops"
    # The non-claim option is preserved under _options.
    assert entry.claims["_options"] == {"no-pty": True}


def test_parse_authorized_keys_line_empty_subject_option_falls_back_to_comment() -> None:
    # An explicit empty subject="" should NOT win — the parser must treat
    # it as not-set and fall back to the comment or fingerprint synthesis.
    line = f'subject="" {_ED25519_SAMPLE} alice@laptop'
    entry = _parse_authorized_keys_line(line)
    assert entry.subject == "alice@laptop"


def test_parse_authorized_keys_line_missing_payload_raises() -> None:
    with pytest.raises(ValueError, match="missing key payload"):
        _parse_authorized_keys_line("ssh-ed25519")


def test_parse_authorized_keys_line_keytype_detection_skips_options_when_first_token_is_keytype() -> None:
    # The first token starts with ``ssh-`` so it must be parsed as the
    # keytype, NOT as an options field.
    line = f"{_ED25519_SAMPLE} alice@laptop"
    entry = _parse_authorized_keys_line(line)
    assert entry.subject == "alice@laptop"
    # No "_options" key should appear when there are no options.
    assert "_options" not in entry.claims


def test_parse_authorized_keys_line_ecdsa_keytype_prefix_recognized() -> None:
    fake_ecdsa = "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTY="
    entry = _parse_authorized_keys_line(f"{fake_ecdsa} svc@oncall")
    assert entry.subject == "svc@oncall"


def test_parse_authorized_keys_line_sk_keytype_prefix_recognized() -> None:
    fake_sk = (
        "sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29tAAAAIA=="
    )
    entry = _parse_authorized_keys_line(f"{fake_sk} yubikey-prod")
    assert entry.subject == "yubikey-prod"
