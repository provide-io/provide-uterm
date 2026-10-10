#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript ``StreamRedactor`` port.

This is the redactor a hosted session's recording runs through when
``recording.redact_sensitive`` is on, which it is by default: the server's
``_build_recording_redactor`` hands ``StreamRedactor(default_rules()).redact``
to every ``SessionLogger`` it opens. A port that recorded without it would
write credentials to disk that the reference writes as markers.

Two things are pinned: what the default rule set rewrites (every rule, the
generic shapes' lookaheads, several secrets in one line, adjacent secrets),
and how the combined pattern picks a replacement when the rules do not all
share one — the ``lastindex`` path the default set takes, and the
single-replacement path a uniform set takes.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_stream_redaction_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path

from provide.uterm.server.bridge.hub.ext import RedactionRule
from provide.uterm.server.bridge.hub.redaction import StreamRedactor
from provide.uterm.server.bridge.hub.redaction_defaults import default_rules

OUT = Path(__file__).with_name("stream_redaction_golden.json")

DEFAULT_INPUTS: list[str] = [
    "",
    "just some normal terminal output with no secrets",
    "aws id AKIAIOSFODNN7EXAMPLE embedded",
    "ASIA0123456789ABCDEF and AROA1234567890ABCDEF",
    # Too short for an access key id: left alone.
    "AKIA0123456789AB",
    "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY here",
    "AWS SECRET ACCESS KEY: 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'",
    "clone with ghp_1234567890abcdefghijklmnopqrstuvwxAB token",
    "slack xoxb-123456789012-123456789012-abcdefghijklmnopqrstuvwx sent",
    "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c ok",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu\n-----END RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY----- after",
    "Authorization: Bearer abc.def-ghi_123+/=",
    "authorization:bearer xyz",
    "login password=hunter2 now",
    "PASSWD: hunter2",
    "pwd='quoted';next",
    "password=",
    "cfg api_key=SECRETVALUE123 loaded",
    "cfg apikey: short",
    "env token=abcdef12345678 set",
    "env token=short set",
    "AKIAIOSFODNN7EXAMPLE and ghp_1234567890abcdefghijklmnopqrstuvwxAB together",
    "AKIAIOSFODNN7EXAMPLE then AROA1234567890ABCDEF",
    "a\r\nAKIAIOSFODNN7EXAMPLE\r\n$ ",
]

# A set whose replacements differ only in the rule that owns the group: a
# nested group in the first rule shifts the second rule's group index, which
# is what the start-index bookkeeping exists to absorb.
CUSTOM_RULES = [
    RedactionRule(pattern=r"(a)(b)c", replacement="<ABC>"),
    RedactionRule(pattern=r"x(y)?z", replacement="<XZ>"),
    RedactionRule(pattern=r"q+", replacement="<Q>"),
]
CUSTOM_INPUTS = ["abc xz xyz qqq", "nothing", "abcabc"]

# Every replacement the same: the single-replacement path.
UNIFORM_RULES = [
    RedactionRule(pattern=r"foo"),
    RedactionRule(pattern=r"ba(r)"),
]
UNIFORM_INPUTS = ["foo bar baz", "none"]


def _run(rules: list[RedactionRule], inputs: list[str]) -> list[dict[str, str]]:
    """Redact each input with one redactor built over ``rules``."""
    redactor = StreamRedactor(rules)
    return [{"input": text, "output": redactor.redact(text)} for text in inputs]


def main() -> int:
    """Write the golden corpus and report the record count."""
    payload = {
        "generator": "packages/provide-uterm-ts/testdata/gen_stream_redaction_golden.py",
        "default_rules": [{"pattern": rule.pattern, "replacement": rule.replacement} for rule in default_rules()],
        "default": _run(default_rules(), DEFAULT_INPUTS),
        "custom_rules": [{"pattern": rule.pattern, "replacement": rule.replacement} for rule in CUSTOM_RULES],
        "custom": _run(CUSTOM_RULES, CUSTOM_INPUTS),
        "uniform_rules": [{"pattern": rule.pattern, "replacement": rule.replacement} for rule in UNIFORM_RULES],
        "uniform": _run(UNIFORM_RULES, UNIFORM_INPUTS),
        "empty": _run([], ["anything at all"]),
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(payload['default'])} default-rule cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
