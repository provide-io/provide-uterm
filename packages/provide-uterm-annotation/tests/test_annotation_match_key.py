#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""``Annotation.match_key``: a hashed (rule, matched text) identity, never serialised."""

from __future__ import annotations

import hashlib

from provide.uterm.annotation import Annotation, PatternDetector, annotation_match_key

_KEY_A = "AKIAIOSFODNN7EXAMPLE"
_KEY_B = "AKIAJ2345678ABCDWXYZ"


def test_the_key_is_sha256_of_rule_id_nul_and_the_full_match() -> None:
    expected = hashlib.sha256(b"cred.aws_access_key\x00" + _KEY_A.encode()).hexdigest()
    assert annotation_match_key("cred.aws_access_key", _KEY_A) == expected
    assert len(expected) == 64


def test_a_lone_surrogate_hashes_instead_of_raising() -> None:
    expected = hashlib.sha256("r\x00\ud800".encode("utf-8", "surrogatepass")).hexdigest()
    assert annotation_match_key("r", "\ud800") == expected


def test_two_different_credentials_of_one_rule_get_different_keys() -> None:
    detector = PatternDetector()
    (first,) = detector.detect("read", f"export K={_KEY_A}", seq=1)
    (second,) = detector.detect("read", f"export K={_KEY_B}", seq=2)
    # Same rule, same description -- only the match key tells them apart.
    assert first.description == second.description
    assert first.match_key == annotation_match_key("cred.aws_access_key", "AKIAIOSFODNN7EXA")
    assert second.match_key == annotation_match_key("cred.aws_access_key", "AKIAJ2345678ABCD")
    assert first.match_key != second.match_key


def test_the_same_match_gets_the_same_key_wherever_it_appears() -> None:
    detector = PatternDetector()
    (first,) = detector.detect("read", "$ sudo ls", seq=1)
    (second,) = detector.detect("send", "later: sudo ls -l", seq=9)
    assert first.match_key == second.match_key


def test_the_key_hashes_the_full_match_not_the_truncated_description() -> None:
    detector = PatternDetector()
    long_url = "curl https://example.com/" + "a" * 200
    annotations = detector.detect("send", long_url, seq=1)
    assert annotations
    assert all(len(a.match_key) == 64 for a in annotations)


def test_the_key_is_not_serialised_and_not_part_of_equality_or_repr() -> None:
    detector = PatternDetector()
    (annotation,) = detector.detect("read", _KEY_A, seq=1)
    assert "match_key" not in annotation.to_dict()
    assert annotation.match_key not in repr(annotation)
    assert _KEY_A not in str(annotation.to_dict())
    other = Annotation(
        label=annotation.label,
        description=annotation.description,
        severity=annotation.severity,
        source=annotation.source,
        principal=annotation.principal,
        span=annotation.span,
    )
    assert other.match_key == ""
    assert other == annotation
