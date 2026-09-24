#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for ``hub.redaction.StreamRedactor`` and ``hub.redaction_defaults.default_rules``.

Two different blind spots, in two different modules:

``StreamRedactor.__init__`` builds two things a happy-path ``redact()`` call
never has to reveal: whether a broken rule silently stops the whole batch
from compiling (it must not -- a bad rule should be skipped, not fatal to the
rules after it), and whether the "all rules share one replacement" fast path
actually engaged. The fast path and the general path produce the *same*
``redact()`` output for a single-rule case, so a mutant that quietly disables
the fast path (leaving ``_single_replacement`` at ``None``) is invisible to
any test that only checks ``redact()``'s return value -- it has to be read
back off the instance directly, the same way ``test_router_redaction_kill.py``
reads ``_REDACT_MAX_DEPTH`` directly. ``_replace_match``'s ``match.lastindex or
0`` fallback is defensive code for a situation ``redact()`` itself can never
produce (every rule is wrapped in its own capturing group, so a match through
the combined pattern always sets ``lastindex``) -- reaching it needs a
hand-built ``re.Match`` with no groups at all, passed to the private method
directly.

``default_rules()`` ships nine rules whose replacement text is asserted only
with ``in``, in ``test_redaction_defaults.py`` -- and ``"[MARKER]" in
"XX[MARKER]XX"`` is ``True``, so wrapping a replacement string in an ``XX``
sentinel on both sides is invisible to a substring check. Each rule below is
fed an input that is an *exact* match for that rule's pattern with nothing
else around it, so ``redact()``'s return value equals the replacement text
verbatim and an ``XX``-wrapped mutant is caught by ``==`` instead of slipping
through ``in``.
"""

from __future__ import annotations

import re

import pytest

from provide.uterm.server.bridge.hub.ext import RedactionRule
from provide.uterm.server.bridge.hub.redaction import StreamRedactor
from provide.uterm.server.bridge.hub.redaction_defaults import default_rules

# ---------------------------------------------------------------------------
# StreamRedactor.__init__ / _replace_match
# ---------------------------------------------------------------------------


def test_a_rule_with_an_invalid_pattern_does_not_stop_later_rules_from_compiling() -> None:
    """The ``except re.error: continue`` must ``continue``, not ``break``.

    An unbalanced group is a compile-time ``re.error`` that the loop is
    documented to skip. If it instead aborted the loop (``break``), every
    rule after the bad one would never be compiled -- here, that means the
    valid rule that follows never redacts anything.
    """
    rules = [
        RedactionRule(pattern="(", replacement="BAD"),
        RedactionRule(pattern=r"secret", replacement="[R]"),
    ]
    redactor = StreamRedactor(rules)

    assert redactor.redact("secret") == "[R]"


def test_pattern_stays_unset_when_no_rule_compiles() -> None:
    """``_pattern`` must start (and stay, when nothing compiles) ``None``, not ``""``.

    ``redact()`` treats both as "nothing to do" identically (both are falsy),
    so this can only be pinned by reading the attribute back directly.
    """
    assert StreamRedactor()._pattern is None
    assert StreamRedactor([RedactionRule(pattern="(", replacement="BAD")])._pattern is None


def test_single_replacement_fast_path_is_recorded_when_every_rule_agrees() -> None:
    """``_single_replacement`` must be set when all compiled rules share one replacement.

    ``redact()`` produces the same text whether the fast path or the general
    ``_replace_match`` path is used for this input, so the only way to see a
    mutant that leaves ``_single_replacement`` at ``None`` (either by
    comparing ``len(set(...))`` against the wrong constant, or by writing
    ``None`` instead of the computed value on the ``== 1`` branch) is to read
    the attribute back directly.
    """
    rules = [
        RedactionRule(pattern="a", replacement="[R]"),
        RedactionRule(pattern="b", replacement="[R]"),
    ]

    assert StreamRedactor(rules)._single_replacement == "[R]"


def test_replace_match_falls_back_to_the_last_rule_when_no_group_matched() -> None:
    """``match.lastindex or 0`` (not ``or 1``) is the fallback when nothing matched.

    Every real ``redact()`` match goes through the combined pattern, where
    each rule is wrapped in its own top-level group, so ``lastindex`` is
    never actually ``None`` in production -- this is defensive code for a
    match object ``redact()`` itself can never construct. Reaching it needs
    a hand-built, group-less match passed straight to the private method.
    """
    rules = [
        RedactionRule(pattern="a", replacement="[FIRST]"),
        RedactionRule(pattern="b", replacement="[SECOND]"),
    ]
    redactor = StreamRedactor(rules)
    groupless_match = re.match(r"x", "x")
    assert groupless_match is not None

    assert redactor._replace_match(groupless_match) == "[SECOND]"


# ---------------------------------------------------------------------------
# default_rules() -- exact replacement text, not just "marker present somewhere"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("clear", "marker"),
    [
        (
            'aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"',  # pragma: allowlist secret
            "[AWS_SECRET_REDACTED]",
        ),
        ("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "[GITHUB_TOKEN_REDACTED]"),  # pragma: allowlist secret
        ("xoxb-12345-67890-abcdefghijklmnopqrstuv", "[SLACK_TOKEN_REDACTED]"),  # pragma: allowlist secret
        (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature_part_here",  # pragma: allowlist secret
            "[JWT_REDACTED]",
        ),
        (
            "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----",  # pragma: allowlist secret
            "[PRIVATE_KEY_REDACTED]",
        ),
        ("Authorization: Bearer eyJhbG.eyJzd.QwE", "Authorization: Bearer [REDACTED]"),
        ("password=hunter2", "[PASSWORD_REDACTED]"),
        ("api_key=abc123def456ghi789", "[API_KEY_REDACTED]"),
        ("token=abcd1234efgh5678ijkl9012", "[TOKEN_REDACTED]"),  # pragma: allowlist secret
    ],
)
def test_default_rule_replacement_text_is_exact_not_xx_wrapped(clear: str, marker: str) -> None:
    """Each input is an exact, whole-string match for one rule's pattern.

    ``redact()``'s return value is therefore the rule's replacement text
    verbatim. A mutant that wraps the replacement in an ``"XX...XX"``
    sentinel still contains ``marker`` as a substring -- which is why the
    existing ``test_default_rules_redact_known_secret_formats`` (an ``in``
    check) never caught it -- but fails this ``==``.
    """
    redactor = StreamRedactor(default_rules())

    assert redactor.redact(clear) == marker
