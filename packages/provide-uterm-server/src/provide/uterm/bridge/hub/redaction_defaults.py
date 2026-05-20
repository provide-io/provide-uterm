#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Default secret-redaction rules for terminal session recordings.

The :class:`~provide.uterm.bridge.hub.redaction.StreamRedactor` is rule-driven
and ships empty by default. That's the right call for a library — a caller
who knows their environment may want a tighter or looser set — but the
common case is "I just want to record sessions without leaking obvious
credentials in plain text". This module supplies a sane default set that
covers the most-impactful real-world credential formats.

Use::

    from provide.uterm.bridge.hub.redaction import StreamRedactor
    from provide.uterm.bridge.hub.redaction_defaults import default_rules

    redactor = StreamRedactor(default_rules())

Or combine with your own rules::

    redactor = StreamRedactor([*default_rules(), my_org_rule])

Every rule is a :class:`~provide.uterm.bridge.hub.ext.RedactionRule` with a
descriptive replacement marker so a reviewer can tell *what* was redacted
without seeing the secret itself.

The patterns prioritise low false-positive rate over completeness — they
target known credential formats with anchored prefixes / canonical
lengths. A determined operator who wants every-credential-everywhere
redaction should layer their own rules on top.
"""

from __future__ import annotations

from provide.uterm.bridge.hub.ext import RedactionRule

# ---------------------------------------------------------------------------
# Cloud-provider credentials
# ---------------------------------------------------------------------------

# AWS access key id — 20-char identifier starting with the AKIA / ASIA /
# AROA / AIDA / AGPA prefix family. See:
#   https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_identifiers.html
_AWS_ACCESS_KEY_ID = r"\b(?:AKIA|ASIA|AROA|AIDA|AGPA|ANPA|ANVA|ASCA)[0-9A-Z]{16}\b"

# AWS secret access key — 40-char base64. Without context it's high false
# positive; pin to the canonical aws_secret_access_key=... form.
# NOTE: ``(?i:...)`` scoped flag (not global ``(?i)``) — global flags
# inside an alternation are rejected by Python's `re` module.
_AWS_SECRET_ACCESS_KEY = r"(?i:aws[_ -]?secret[_ -]?access[_ -]?key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}['\"]?)"  # noqa: S105 — variable name matches the redaction *pattern*, not a real secret

# ---------------------------------------------------------------------------
# GitHub credentials
# ---------------------------------------------------------------------------

# GitHub personal access token (classic + fine-grained). All start with a
# 4-char ghX_ prefix; the rest is alphanumeric/underscore of fixed length.
_GITHUB_TOKEN = r"\bgh[opusr]_[A-Za-z0-9_]{36,251}\b"  # noqa: S105

# GitHub Actions OIDC token — JWT, but easily spotted via the leading
# `eyJ` of the base64 header. JWT_PATTERN below catches this too.

# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

# Slack tokens: xoxb-, xoxa-, xoxp-, xoxr-, xoxs-, xoxe- followed by
# numeric workspace/user/tail ids. See:
#   https://api.slack.com/authentication/token-types
_SLACK_TOKEN = r"\bxox[abeprs]-(?:[0-9]+-){2,}[A-Za-z0-9-]{20,}\b"  # noqa: S105

# ---------------------------------------------------------------------------
# Generic shapes
# ---------------------------------------------------------------------------

# JSON Web Token — three base64url-encoded segments joined by dots. We
# only redact when the header decodes plausibly (starts with eyJ which
# is base64url of `{"`). Body and signature lengths vary.
_JWT = r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"

# SSH/PEM private key blocks. We match the BEGIN/END marker pair as a
# whole multi-line region.
_PEM_PRIVATE_KEY = (
    r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"
    r"[\s\S]+?"
    r"-----END (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"
)

# `Authorization: Bearer <token>` HTTP headers — the value is everything
# after the prefix up to the next whitespace.
_BEARER_HEADER = r"(?i:\bauthorization\s*:\s*bearer\s+([A-Za-z0-9._\-+/=]+))"

# `password=...` / `passwd=...` / `pwd=...` shapes in command lines or
# config snippets. Limit the value to non-whitespace, max 128 chars so we
# don't gobble paragraphs. Allow ``key: value`` and ``key=value``.
_GENERIC_PASSWORD = r"(?i:\b(?:password|passwd|pwd)\s*[:=]\s*['\"]?(\S{1,128}?)['\"]?(?=\s|$|,|;|&))"  # noqa: S105

# `api[_-]key=...` shapes.
_GENERIC_API_KEY = r"(?i:\bapi[_-]?key\s*[:=]\s*['\"]?(\S{6,128}?)['\"]?(?=\s|$|,|;|&))"

# `token=...` shape (loose; matches a lot but bounded length).
_GENERIC_TOKEN = r"(?i:\btoken\s*[:=]\s*['\"]?(\S{8,256}?)['\"]?(?=\s|$|,|;|&))"  # noqa: S105


def default_rules() -> list[RedactionRule]:
    """Return the canonical default set of recording-redaction rules.

    Stable order: scoped/high-confidence patterns first (AWS keys, GitHub
    tokens, Slack tokens, JWTs, PEM blocks), generic shapes (Authorization
    headers, password=, api_key=, token=) last. The redactor builds a
    single combined regex, so the order has no effect on correctness; it
    matters only when reading the source.
    """
    return [
        # High-confidence, anchored credential formats.
        RedactionRule(pattern=_AWS_ACCESS_KEY_ID, replacement="[AWS_ACCESS_KEY_REDACTED]"),
        RedactionRule(pattern=_AWS_SECRET_ACCESS_KEY, replacement="[AWS_SECRET_REDACTED]"),
        RedactionRule(pattern=_GITHUB_TOKEN, replacement="[GITHUB_TOKEN_REDACTED]"),
        RedactionRule(pattern=_SLACK_TOKEN, replacement="[SLACK_TOKEN_REDACTED]"),
        RedactionRule(pattern=_JWT, replacement="[JWT_REDACTED]"),
        RedactionRule(pattern=_PEM_PRIVATE_KEY, replacement="[PRIVATE_KEY_REDACTED]"),
        # Generic shapes — broader patterns; placed after the specific ones
        # so a reviewer comparing the regex order sees the high-confidence
        # rules first.
        RedactionRule(pattern=_BEARER_HEADER, replacement="Authorization: Bearer [REDACTED]"),
        RedactionRule(pattern=_GENERIC_PASSWORD, replacement="[PASSWORD_REDACTED]"),
        RedactionRule(pattern=_GENERIC_API_KEY, replacement="[API_KEY_REDACTED]"),
        RedactionRule(pattern=_GENERIC_TOKEN, replacement="[TOKEN_REDACTED]"),
    ]


__all__ = ["default_rules"]
