#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript annotation port.

This is what marks the interesting moments in a recording — a credential that
appeared on screen, a privilege escalation, a destructive command. Getting it
wrong is quiet in both directions: a missed match means an incident review
never sees the moment, and a leaked one means the *secret itself* ends up in
the annotation, which flows to telemetry and logs.

Three behaviours carry the weight:

* **One annotation per category.** The rules are ordered most-specific first,
  and the first to match a category wins. Without that a single line mentioning
  a password produces four near-identical annotations and buries the timeline.
* **A description that never contains what it could not format.** When a
  template is malformed the fallback deliberately omits the match rather than
  interpolating it, because the match is the secret.
* **No credential in a description.** A template that embeds ``{match}``
  receives the match with every credential-shaped substring replaced by
  ``[SECRET]``, masked BEFORE the 80-character cut (``_masking``). ``conn.curl``
  and ``conn.wget`` match from the command to the last URL scheme on the line,
  so without it every header, ``-u`` credential and form field on a curl line
  was copied into the recording.
* **Bridging a chunk boundary.** A detector that scans one chunk at a time
  misses an AWS key split across two reads. The streaming wrapper carries a
  bounded tail — but only the part *after* the furthest match, so a completed
  match is not reported twice while a second secret starting right after it
  still bridges.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_annotation_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.annotation._detector import PatternDetector
from provide.uterm.annotation._masking import SECRET_MARKER, SECRET_MASKS, mask_secrets
from provide.uterm.annotation._models import Annotation, AnnotationSpan, DetectionRule
from provide.uterm.annotation._rules import BUILTIN_RULES
from provide.uterm.annotation._streaming import StreamingDetector

OUT = Path(__file__).with_name("annotation_golden.json")

AWS_KEY = "AKIA" + "ABCDEFGHIJKL"

# Credentials a command line can carry. None may reach a description.
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"  # pragma: allowlist secret
FULL_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # pragma: allowlist secret
GH_TOKEN = "ghp_" + "Ab1" * 12  # pragma: allowlist secret
PASSWORD = "hunter2-correct-horse"  # noqa: S105  # pragma: allowlist secret

# (name, event_type, text) — what a single scan makes of one chunk.
SCAN_CASES: list[tuple[str, str, str]] = [
    ("nothing interesting", "read", "total 24\ndrwxr-xr-x  4 alice staff"),
    ("empty", "read", ""),
    ("an aws key", "read", f"export AWS_ACCESS_KEY_ID={AWS_KEY}"),
    ("a github token", "read", "gh_p token: ghp_abcdefgh"),
    ("a bearer token", "read", "Authorization: Bearer abcdefghijkl"),
    ("a private key header", "read", "-----BEGIN RSA PRIVATE KEY-----"),
    ("a secret assignment", "read", "PASSWORD = hunter2"),
    ("sudo", "send", "sudo systemctl restart nginx"),
    ("su dash", "send", "su - root"),
    ("two categories at once", "send", f"sudo echo {AWS_KEY}"),
    ("the same category twice", "read", f"{AWS_KEY} and ghp_abcdefgh"),
    ("a match at the very start", "read", AWS_KEY),
    ("a match at the very end", "read", f"trailing {AWS_KEY}"),
    # The credential rule comes first in the rule list but matches *earlier*
    # here, so the furthest match belongs to a later rule. A scanner that kept
    # the first offset would carry text a later rule already consumed.
    ("a later rule matches further", "send", f"{AWS_KEY} then sudo reboot"),
    ("a very long match", "read", "-----BEGIN OPENSSH PRIVATE KEY-----" + "x" * 200),
    ("an event type nothing applies to", "resize", f"sudo {AWS_KEY}"),
    # Rules that embed {match} see it masked, then cut.
    ("a bearer header on a curl line", "send", f"curl -H 'Authorization: Bearer {JWT}' https://api.example.com/v1"),
    ("a bare bearer on a curl line", "send", f"curl -H 'X-Auth: Bearer {JWT}' https://api.example.com"),
    ("an aws key on a curl line", "send", f"curl -d key={FULL_AWS_KEY} https://x.example"),
    ("a github token on a wget line", "send", f"wget --header=X-Gh:{GH_TOKEN} https://api.github.com/x"),
    ("a password assignment on a curl line", "send", f"curl -d 'user=bob&password={PASSWORD}' https://login.example"),
    ("a curl -u credential", "send", f"curl -u alice:{PASSWORD} https://x.example"),
    ("url userinfo before the last url", "send", f"curl https://alice:{PASSWORD}@a.example/x -o y https://b.example"),
    ("the cut would have split a key", "send", "curl -d " + "x" * 61 + "=" + FULL_AWS_KEY + " https://x.example"),
]

# mask_secrets on its own: every mask, and what each must leave alone. The
# non-ASCII cases pin that the four regex engines agree (no \s, no (?i)).
MASK_CASES: list[str] = [
    "",
    "nothing to see here",
    "tokens: 5",
    "AKIA1234",
    "gho_short",
    "user@host",
    f"x {FULL_AWS_KEY} y {FULL_AWS_KEY}",
    "Token = 'abc' rest",
    '"api_key": "k-123"',
    "API-KEY:v",
    "passwd:pw secret=s",
    "basic dXNlcjpwYXNz",
    "Authorization: Bearer abc def",
    "authorization:token abc def",
    f"jwt {JWT}",
    "ftp://bob:pw@h",
    "-U proxy:pw",
    "-ualice:pw",
    "--proxy-user p:q --ftp-password=z --proxy-password y",
    "--user-agent foo",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    f"{GH_TOKEN}ghs_{'z' * 8}",
    SECRET_MARKER,
    # The Kelvin sign and the long s fold to k/s under CPython's and RE2's (?i)
    # but not ECMAScript's: spelled-out case keeps all four engines agreeing.
    "to\u212aen=abc pa\u017fsword=abc",
    # Only ASCII whitespace separates a scheme from its value, in every engine.
    "Bearer\u00a0abc Bearer\tdef",
    "caf\u00e9 token=\u00e9t\u00e9 \U0001f600 AKIA0123456789ABCDEF",
]


# (name, chunks) — a stream, one chunk per detect() call.
STREAM_CASES: list[tuple[str, list[str]]] = [
    ("a key split down the middle", ["export KEY=AKIAABC", "DEFGHIJKL and more"]),
    ("a key wholly inside one chunk", [f"export KEY={AWS_KEY}", " and more"]),
    ("nothing at all", ["ls -la", "total 24"]),
    ("a second key right after the first", [f"{AWS_KEY} AKIAZZZ", "ZZZZZZZZZ"]),
    ("the same chunk twice", [f"{AWS_KEY}", f"{AWS_KEY}"]),
    ("an empty chunk in the middle", ["AKIAABC", "", "DEFGHIJKL"]),
    ("a match that completes on the third", ["AKIA", "ABCDEF", "GHIJKL"]),
    # Only the tail after the furthest match is carried, so the second chunk
    # does not see the part of the first that a rule already consumed.
    ("a key then a partial key", [f"{AWS_KEY} AKIAABCDEFGH", "IJKL"]),
]


def _describe(annotations: list[Annotation]) -> list[dict[str, Any]]:
    """The annotations, as a client sees them."""
    return [annotation.to_dict() for annotation in annotations]


def _record_scans() -> list[dict[str, Any]]:
    """Drive a single-chunk scan for every case."""
    detector = PatternDetector()
    records = []
    for name, event_type, text in SCAN_CASES:
        annotations, end = detector.scan(event_type, text, 7)
        records.append(
            {
                "name": name,
                "event_type": event_type,
                "text": text,
                "annotations": _describe(annotations),
                "match_end": end,
                "detect_matches_scan": _describe(detector.detect(event_type, text, 7)) == _describe(annotations),
            }
        )
    return records


def _record_streams() -> list[dict[str, Any]]:
    """Drive a stream for every case, recording what each chunk produced."""
    records = []
    for name, chunks in STREAM_CASES:
        streaming = StreamingDetector(PatternDetector())
        steps = []
        for index, chunk in enumerate(chunks):
            steps.append({"chunk": chunk, "annotations": _describe(streaming.detect("read", chunk, index))})
        records.append({"name": name, "steps": steps})
    return records


def _record_bounded_carry() -> dict[str, Any]:
    """A carry too small to bridge, and one large enough."""
    tight = StreamingDetector(PatternDetector(), max_carry=4)
    tight.detect("read", "zzzzAKIAABCDEFGH", 0)
    tight_result = _describe(tight.detect("read", "IJKL", 1))

    roomy = StreamingDetector(PatternDetector(), max_carry=64)
    roomy.detect("read", "zzzzAKIAABCDEFGH", 0)
    roomy_result = _describe(roomy.detect("read", "IJKL", 1))
    return {"too_small_to_bridge": tight_result, "large_enough": roomy_result}


def _record_carry() -> dict[str, Any]:
    """How much of a chunk is carried, and what a reset does."""
    streaming = StreamingDetector(PatternDetector(), max_carry=8)
    streaming.detect("read", "0123456789abcdef", 0)
    bounded = streaming.detect("read", "", 1)

    resetting = StreamingDetector(PatternDetector())
    resetting.detect("read", "AKIAABC", 0)
    resetting.reset()
    after_reset = _describe(resetting.detect("read", "DEFGHIJKL", 1))

    without_reset = StreamingDetector(PatternDetector())
    without_reset.detect("read", "AKIAABC", 0)
    kept = _describe(without_reset.detect("read", "DEFGHIJKL", 1))

    return {
        "an_empty_chunk_produces_nothing": _describe(bounded),
        "a_reset_forgets_the_tail": after_reset,
        "without_a_reset_it_bridges": kept,
        "default_max_carry": 512,
    }


def _record_masks() -> dict[str, Any]:
    """The masking rule: its marker, its patterns in order, and what it makes of each case."""
    return {
        "marker": SECRET_MARKER,
        "patterns": [mask.pattern for mask in SECRET_MASKS],
        "cases": [{"text": text, "masked": mask_secrets(text)} for text in MASK_CASES],
    }


def _record_templates() -> dict[str, Any]:
    """What a malformed description template does."""
    good = DetectionRule(
        rule_id="t.good",
        label="test",
        pattern=__import__("re").compile("secret-value"),
        severity="low",
        description_template="found {match} in {event_type}",
        event_types=frozenset({"read"}),
        category="test",
    )
    broken = DetectionRule(
        rule_id="t.broken",
        label="test",
        pattern=__import__("re").compile("secret-value"),
        severity="low",
        description_template="found {nonexistent}",
        event_types=frozenset({"read"}),
        category="test",
    )
    positional = DetectionRule(
        rule_id="t.positional",
        label="test",
        pattern=__import__("re").compile("secret-value"),
        severity="low",
        description_template="found {0}",
        event_types=frozenset({"read"}),
        category="test",
    )
    long_match = DetectionRule(
        rule_id="t.long",
        label="test",
        pattern=__import__("re").compile("x+"),
        severity="low",
        description_template="found {match}",
        event_types=frozenset({"read"}),
        category="test",
    )
    return {
        "a_good_template": PatternDetector([good]).detect("read", "a secret-value here", 1)[0].description,
        "a_missing_key": PatternDetector([broken]).detect("read", "a secret-value here", 1)[0].description,
        "a_positional_field": PatternDetector([positional]).detect("read", "a secret-value here", 1)[0].description,
        "a_long_match_is_truncated": PatternDetector([long_match]).detect("read", "x" * 200, 1)[0].description,
        "truncate_at": 80,
    }


def main() -> int:
    """Write the golden corpus and report the case count."""
    corpus = {
        "aws_key": AWS_KEY,
        "rules": [
            {
                "rule_id": rule.rule_id,
                "label": rule.label,
                "pattern": rule.pattern.pattern,
                "severity": rule.severity,
                "description_template": rule.description_template,
                "event_types": sorted(rule.event_types),
                "category": rule.category,
            }
            for rule in BUILTIN_RULES
        ],
        "categories_in_order": list(dict.fromkeys(rule.category for rule in BUILTIN_RULES)),
        "scans": _record_scans(),
        "streams": _record_streams(),
        "carry": _record_carry(),
        "bounded_carry": _record_bounded_carry(),
        "templates": _record_templates(),
        "masks": _record_masks(),
        "empty_annotation": Annotation(label="l", description="d", severity="s", source="src", principal="p").to_dict(),
        "annotation_with_span": Annotation(
            label="l", description="d", severity="s", source="src", principal="p", span=AnnotationSpan(1, 2)
        ).to_dict(),
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(corpus['rules'])} rules, {len(SCAN_CASES)} scans, {len(STREAM_CASES)} streams)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
