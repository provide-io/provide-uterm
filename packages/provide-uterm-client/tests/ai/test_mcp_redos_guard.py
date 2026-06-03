#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Structural ReDoS-guard tests for the user-supplied-regex compiler.

The MCP tool surface lets a (potentially viewer-role) caller supply a regex
that the client compiles with stdlib ``re``.  A pure length cap does not bound
catastrophic backtracking for short pathological patterns (e.g. ``(a+)+$``), so
``_compile_user_pattern`` additionally rejects nested-quantifier and
quantified-backreference constructs before ``re.compile`` is ever reached.
"""

from __future__ import annotations

import pytest

from provide.uterm.ai.patterns import has_catastrophic_construct
from provide.uterm.ai.server_impl import _compile_user_pattern

# ---------------------------------------------------------------------------
# has_catastrophic_construct — structural classifier
# ---------------------------------------------------------------------------


class TestHasCatastrophicConstruct:
    @pytest.mark.parametrize(
        "pattern",
        [
            r"(a+)+$",
            r"(a*)*",
            r"(a+)*",
            r"(a*)+",
            r"(a+)+",
            r"([a-z]+)+",
            r"(\w+)+$",
            r"(.*)*",
            r"(\d+)+",
            r"(a{1,5})+",
            r"(a+?)+",  # lazy inner quantifier still nests
            r"(\1)+",  # group wrapping a backreference, repeated
            r"\1+",  # quantified backreference directly
            r"\2*",
        ],
    )
    def test_flags_catastrophic(self, pattern: str) -> None:
        assert has_catastrophic_construct(pattern) is True

    @pytest.mark.parametrize(
        "pattern",
        [
            r"foo.*bar",
            r"\$",
            r"a+b+c+",
            r"(abc)+",
            r"(ab|cd)+",
            r"(?:abc)+def",
            r"[a-z]+@[a-z]+",
            r"^prompt> $",
            r"\d{1,3}\.\d{1,3}",
            r"(foo)\1",  # backref present but not quantified
            r"a\*+",  # escaped star inside group body is literal
            r"()+",  # empty-body group, quantified — not a repeated unit
            r"",  # empty pattern is harmless
        ],
    )
    def test_allows_safe(self, pattern: str) -> None:
        assert has_catastrophic_construct(pattern) is False

    def test_unbalanced_close_paren_does_not_crash(self) -> None:
        # A stray ``)`` with no matching ``(`` must not raise; the stdlib
        # compiler downstream will reject it as a bad pattern instead.
        assert has_catastrophic_construct(")a+)+") is False

    def test_escaped_close_paren_is_not_a_group(self) -> None:
        # ``\)`` is a literal paren, so the following ``+`` does not make a
        # nested-quantifier group.
        assert has_catastrophic_construct(r"a\)+") is False


# ---------------------------------------------------------------------------
# _compile_user_pattern — structural guard wired into the compiler
# ---------------------------------------------------------------------------


class TestCompileUserPatternRejectsCatastrophic:
    def test_rejects_nested_quantifier(self) -> None:
        with pytest.raises(ValueError, match="catastrophic|pattern"):
            _compile_user_pattern("(a+)+$")

    def test_rejects_quantified_backreference(self) -> None:
        with pytest.raises(ValueError, match="catastrophic|pattern"):
            _compile_user_pattern(r"(\1)+")

    def test_still_accepts_safe_pattern(self) -> None:
        compiled = _compile_user_pattern(r"prompt> $")
        assert compiled.search("prompt> ") is not None

    def test_length_cap_still_enforced(self) -> None:
        with pytest.raises(ValueError, match="too long"):
            _compile_user_pattern("x" * 2000)
