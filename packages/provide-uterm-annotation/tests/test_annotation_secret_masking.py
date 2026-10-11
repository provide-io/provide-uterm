#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""No annotation description carries credential material.

The credential rules never embed their match (their templates name only
``{event_type}``), so the AKIA/``gh?_``/``Bearer`` rules are not where secrets
escape. The leak is in the rules that DO embed ``{match}`` and whose pattern
spans free-form text: ``conn.curl`` and ``conn.wget`` (``\\bcurl\\b.*https?://``)
match from the command name to the LAST URL scheme on the line, so every
header, ``-u`` credential, form field and earlier URL on a curl/wget command
line was copied into the description -- and the recording writes annotation
descriptions verbatim (``SessionLogger.log_event`` applies no redactor).

The 80-character cut made it worse, not better: it can stop inside a key, and
the fragment that remains is shorter than any redaction rule recognises.

The fix masks credential-shaped substrings of the match BEFORE the cut (see
``provide.uterm.annotation._masking``). Detection and ``match_key`` are
unchanged: the key still hashes the full raw match.
"""

from __future__ import annotations

import re

import pytest

from provide.uterm.annotation import (
    BUILTIN_RULES,
    SECRET_MARKER,
    PatternDetector,
    annotation_match_key,
    mask_secrets,
)

BEARER = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"  # pragma: allowlist secret
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # pragma: allowlist secret
GH_TOKEN = "ghp_" + "Ab1" * 12  # pragma: allowlist secret
PASSWORD = "hunter2-correct-horse"  # pragma: allowlist secret


def _description(event_type: str, text: str, rule_id: str) -> str:
    rule = next(r for r in BUILTIN_RULES if r.rule_id == rule_id)
    [annotation] = PatternDetector([rule]).detect(event_type, text, 1)
    return annotation.description


# ---------------------------------------------------------------------------
# The leaks: each of these carried the secret before the fix.
# ---------------------------------------------------------------------------


def test_bearer_header_on_a_curl_line_is_masked() -> None:
    text = f"curl -H 'Authorization: Bearer {BEARER}' https://api.example.com/v1"
    description = _description("send", text, "conn.curl")
    assert BEARER not in description
    assert BEARER[:8] not in description
    assert description == "curl HTTP request detected: curl -H 'Authorization: [SECRET]' https://"


def test_bare_bearer_without_authorization_prefix_is_masked() -> None:
    text = f"curl -H 'X-Auth: Bearer {BEARER}' https://api.example.com"
    description = _description("send", text, "conn.curl")
    assert BEARER[:8] not in description
    assert "Bearer [SECRET]" in description


def test_aws_key_on_a_command_line_is_masked() -> None:
    text = f"curl -d key={AWS_KEY} https://x.example"
    description = _description("send", text, "conn.curl")
    assert "AKIA" not in description
    assert description == "curl HTTP request detected: curl -d key=[SECRET] https://"


def test_github_token_on_a_wget_line_is_masked() -> None:
    text = f"wget --header=X-Gh:{GH_TOKEN} https://api.github.com/x"
    description = _description("send", text, "conn.wget")
    assert "ghp_" not in description
    assert description == "wget HTTP request detected: wget --header=X-Gh:[SECRET] https://"


def test_password_assignment_on_a_curl_line_is_masked() -> None:
    text = f"curl -d 'user=bob&password={PASSWORD}' https://login.example"
    description = _description("send", text, "conn.curl")
    assert PASSWORD not in description
    assert PASSWORD[:4] not in description


@pytest.mark.parametrize(
    "text",
    [
        f"curl -u alice:{PASSWORD} https://x.example",
        f"curl -ualice:{PASSWORD} https://x.example",
        f"curl --user alice:{PASSWORD} https://x.example",
        f"wget --password {PASSWORD} https://x.example",
        f"wget --http-password={PASSWORD} https://x.example",
        f"curl https://alice:{PASSWORD}@a.example/x -o y https://b.example",
    ],
)
def test_curl_and_wget_credential_carriers_are_masked(text: str) -> None:
    rule_id = "conn.wget" if text.startswith("wget") else "conn.curl"
    description = _description("send", text, rule_id)
    assert PASSWORD not in description
    assert PASSWORD[:4] not in description
    assert SECRET_MARKER in description


def test_the_80_char_cut_can_no_longer_leave_a_partial_key() -> None:
    # Before the fix the cut landed inside the key: the description ended in
    # "AKIAIOSFOD", a fragment no 20-character AWS redaction rule matches.
    pad = "x" * (80 - len("curl -d ") - 10)
    text = f"curl -d {pad}{AWS_KEY} https://x.example"
    description = _description("send", text, "conn.curl")
    assert "AKIA" not in description
    assert len(description) == len("curl HTTP request detected: ") + 80


def test_private_key_body_is_masked_but_the_header_is_kept() -> None:
    body = "MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn"  # pragma: allowlist secret
    assert mask_secrets(f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END") == (
        "-----BEGIN RSA PRIVATE KEY-----[SECRET]-----END"
    )
    # The header-only rule's own description is unchanged.
    assert (
        _description("read", "-----BEGIN RSA PRIVATE KEY-----", "cred.private_key_header")
        == "Private key header detected: -----BEGIN RSA PRIVATE KEY-----"
    )


# ---------------------------------------------------------------------------
# What does NOT change.
# ---------------------------------------------------------------------------


def test_match_key_still_hashes_the_full_raw_match() -> None:
    text = f"curl -H 'Authorization: Bearer {BEARER}' https://api.example.com"
    [annotation] = PatternDetector([r for r in BUILTIN_RULES if r.rule_id == "conn.curl"]).detect("send", text, 1)
    raw = re.search(r"\bcurl\b.*https?://", text)
    assert raw is not None
    assert annotation.match_key == annotation_match_key("conn.curl", raw.group(0))


@pytest.mark.parametrize(
    ("text", "rule_id", "expected"),
    [
        (AWS_KEY, "cred.aws_access_key", "AWS access key detected in read"),
        (GH_TOKEN, "cred.github_token", "GitHub token detected in read"),
        (f"Bearer {BEARER}", "cred.bearer_token", "Bearer token detected in read"),
        (f"password={PASSWORD}", "cred.generic_secret", "Secret assignment detected in read"),
        ("sudo rm -rf /", "esc.sudo", "sudo command detected: sudo"),
        ("curl https://example.com/install.sh", "conn.curl", "curl HTTP request detected: curl https://"),
    ],
)
def test_detection_and_harmless_descriptions_are_unchanged(text: str, rule_id: str, expected: str) -> None:
    assert _description("read", text, rule_id) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ""),
        ("nothing to see here", "nothing to see here"),
        ("tokens: 5", "tokens: 5"),
        ("AKIA1234", "AKIA1234"),
        ("gho_short", "gho_short"),
        ("user@host", "user@host"),
        (f"x {AWS_KEY} y {AWS_KEY}", "x [SECRET] y [SECRET]"),
        ("Token = 'abc' rest", "Token = '[SECRET]' rest"),
        ('"api_key": "k-123"', '"api_key": "[SECRET]"'),
        ("API-KEY:v", "API-KEY:[SECRET]"),
        ("basic dXNlcjpwYXNz", "basic [SECRET]"),
        ("authorization:token abc def", "authorization:[SECRET]"),
        (f"jwt {BEARER}", "jwt [SECRET]"),
        ("ftp://bob:pw@h", "ftp://[SECRET]@h"),
        ("-U proxy:pw", "-U [SECRET]"),
        ("--user-agent foo", "--user-agent foo"),
        ("[SECRET]", "[SECRET]"),
    ],
)
def test_mask_secrets_shapes(text: str, expected: str) -> None:
    assert mask_secrets(text) == expected


def test_masking_is_idempotent() -> None:
    text = f"curl -u a:{PASSWORD} -H 'Authorization: Bearer {BEARER}' -d token={AWS_KEY} https://"
    once = mask_secrets(text)
    assert mask_secrets(once) == once
