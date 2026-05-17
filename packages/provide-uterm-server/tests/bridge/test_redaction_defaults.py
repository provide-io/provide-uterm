#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Verify the default redaction ruleset blocks the credential formats it claims to."""

from __future__ import annotations

import pytest

from provide.uterm.bridge.hub.redaction import StreamRedactor
from provide.uterm.bridge.hub.redaction_defaults import default_rules


@pytest.fixture()
def redactor() -> StreamRedactor:
    return StreamRedactor(default_rules())


@pytest.mark.parametrize(
    ("clear", "redaction_marker"),
    [
        ("AKIAIOSFODNN7EXAMPLE", "[AWS_ACCESS_KEY_REDACTED]"),
        ("export AKIAQWERTYUIOPASDFGH", "[AWS_ACCESS_KEY_REDACTED]"),
        ("ASIAIOSFODNN7EXAMPLE", "[AWS_ACCESS_KEY_REDACTED]"),
        (
            'aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"',
            "[AWS_SECRET_REDACTED]",
        ),
        ("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "[GITHUB_TOKEN_REDACTED]"),
        ("gho_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "[GITHUB_TOKEN_REDACTED]"),
        ("ghs_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "[GITHUB_TOKEN_REDACTED]"),
        ("xoxb-12345-67890-abcdefghijklmnopqrstuv", "[SLACK_TOKEN_REDACTED]"),
        (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature_part_here",
            "[JWT_REDACTED]",
        ),
        ("password=hunter2", "[PASSWORD_REDACTED]"),
        ('PASSWORD="hunter2"', "[PASSWORD_REDACTED]"),
        ("pwd: hunter2", "[PASSWORD_REDACTED]"),
        ("api_key=abc123def456ghi789", "[API_KEY_REDACTED]"),
        ("API-KEY: xyz_secret_value_12345", "[API_KEY_REDACTED]"),
        ("token=abcd1234efgh5678ijkl9012", "[TOKEN_REDACTED]"),
        (
            "Authorization: Bearer eyJhbG.eyJzd.QwE",
            "Authorization: Bearer [REDACTED]",
        ),
    ],
)
def test_default_rules_redact_known_secret_formats(
    redactor: StreamRedactor, clear: str, redaction_marker: str
) -> None:
    out = redactor.redact(clear)
    assert redaction_marker in out, f"expected {redaction_marker!r} in {out!r}"


def test_pem_private_key_block_is_redacted(redactor: StreamRedactor) -> None:
    pem = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
        "QyNTUxOQAAACDB+...\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    out = redactor.redact(f"prelude\n{pem}\nepilogue")
    assert "[PRIVATE_KEY_REDACTED]" in out
    assert "BEGIN OPENSSH" not in out
    assert "prelude" in out
    assert "epilogue" in out


@pytest.mark.parametrize(
    "clear",
    [
        "ssh user@host",
        "uterm server --config server.toml",
        "AKIA",
        "AKIA1234",
        "ghp_short",
        "xoxb-only-one-segment",
        "the password is on the post-it",
    ],
)
def test_default_rules_do_not_redact_innocuous_text(
    redactor: StreamRedactor, clear: str
) -> None:
    assert redactor.redact(clear) == clear


def test_default_rules_load_without_errors() -> None:
    rules = default_rules()
    assert len(rules) >= 10, "fewer rules than expected; check for silent drops"
    for rule in rules:
        assert "[" in rule.replacement
        assert "]" in rule.replacement
        assert "\\1" not in rule.replacement
