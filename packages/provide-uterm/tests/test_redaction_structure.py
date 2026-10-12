#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""``redact_structure`` — the one walker the session recorder and the hub share.

Three properties matter more than the rest, and each is pinned to the exact
boundary here because a near miss is a leak:

* the depth cap: no input can raise ``RecursionError``, and a container at the
  cap is replaced whole (never returned unwalked);
* key-aware masking: a bare ``{"token": "..."}`` has no ``token=`` context for
  the text rules, so the field name has to decide;
* the input is never mutated, and tuples come back as lists (as JSON would).
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from provide.uterm.redaction import (
    DEPTH_LIMIT_MARKER,
    NON_SECRET_KEY_SUFFIXES,
    REDACT_MAX_DEPTH,
    SENSITIVE_KEY_MARKER,
    SENSITIVE_KEY_PAIRS,
    SENSITIVE_KEY_SEGMENTS,
    is_sensitive_key,
    key_segments,
    make_redactor,
    redact_structure,
)

_R = make_redactor([r"S3cret\w*"])


def _nest(depth: int, *, container: str, leaf: Any = "S3cretLeaf") -> Any:
    value: Any = leaf
    for _ in range(depth):
        if container == "dict":
            value = {"k": value}
        elif container == "tuple":
            value = (value,)
        else:
            value = [value]
    return value


def _innermost(value: Any) -> Any:
    while isinstance(value, (dict, list, tuple)):
        value = value["k"] if isinstance(value, dict) else value[0]
    return value


def test_the_constants_are_the_values_the_ports_mirror() -> None:
    """Go, TypeScript and C# hard-code these; a change here must be a change there."""
    assert REDACT_MAX_DEPTH == 32
    assert DEPTH_LIMIT_MARKER == "[NESTED_REDACTED]"
    assert SENSITIVE_KEY_MARKER == "[SECRET_REDACTED]"
    assert (
        frozenset(
            {
                "token",
                "secret",
                "password",
                "passwd",
                "passphrase",
                "credential",
                "credentials",
                "cookie",
                "authorization",
                "apikey",
            }
        )
        == SENSITIVE_KEY_SEGMENTS
    )
    assert frozenset({("api", "key"), ("private", "key"), ("access", "key")}) == SENSITIVE_KEY_PAIRS
    assert (
        frozenset(
            {
                "count",
                "len",
                "length",
                "size",
                "type",
                "kind",
                "ttl",
                "expiry",
                "expires",
                "hash",
                "required",
                "enabled",
            }
        )
        == NON_SECRET_KEY_SUFFIXES
    )


def test_strings_are_redacted_at_any_level() -> None:
    assert redact_structure("a S3cretX b", _R) == "a [REDACTED] b"
    assert redact_structure({"a": ["S3cret1", {"b": "S3cret2"}]}, _R) == {"a": ["[REDACTED]", {"b": "[REDACTED]"}]}


@pytest.mark.parametrize("scalar", [0, 7, 0.5, True, False, None])
def test_non_string_scalars_are_returned_unchanged(scalar: Any) -> None:
    assert redact_structure(scalar, _R) is scalar
    assert redact_structure({"v": scalar}, _R)["v"] is scalar


def test_tuples_are_walked_and_become_lists() -> None:
    out = redact_structure({"t": ("keep", "S3cret9")}, _R)
    assert out == {"t": ["keep", "[REDACTED]"]}
    assert type(out["t"]) is list
    assert redact_structure(("S3cret",), _R) == ["[REDACTED]"]


def test_the_input_is_not_mutated_and_containers_are_rebuilt() -> None:
    inner = ["S3cret1"]
    data = {"x": inner, "d": {"y": "S3cret2"}}
    out = redact_structure(data, _R)
    assert data == {"x": ["S3cret1"], "d": {"y": "S3cret2"}}
    assert out["x"] is not inner
    assert out is not data


def test_unknown_objects_pass_through() -> None:
    marker = object()
    assert redact_structure(marker, _R) is marker


@pytest.mark.parametrize("container", ["dict", "list", "tuple"])
def test_a_secret_inside_the_last_walked_container_is_redacted(container: str) -> None:
    """``REDACT_MAX_DEPTH`` containers: the deepest is at level 31, so it is walked."""
    out = redact_structure(_nest(REDACT_MAX_DEPTH, container=container), _R)
    assert _innermost(out) == "[REDACTED]"


@pytest.mark.parametrize("container", ["dict", "list", "tuple"])
def test_a_container_at_the_cap_is_replaced_by_the_marker(container: str) -> None:
    out = redact_structure(_nest(REDACT_MAX_DEPTH + 1, container=container), _R)
    assert _innermost(out) == DEPTH_LIMIT_MARKER
    assert "S3cretLeaf" not in json.dumps(out)
    # Exactly REDACT_MAX_DEPTH containers survive above the marker.
    levels = 0
    cur = out
    while isinstance(cur, (dict, list)):
        levels += 1
        cur = cur["k"] if isinstance(cur, dict) else cur[0]
    assert levels == REDACT_MAX_DEPTH


def test_a_string_at_the_cap_is_still_redacted_not_replaced() -> None:
    """Only containers are cut off; a scalar sitting at the cap is handled as usual."""
    out = redact_structure(_nest(REDACT_MAX_DEPTH, container="list", leaf=5), _R)
    assert _innermost(out) == 5


def test_max_depth_is_honoured_and_propagated() -> None:
    assert redact_structure({"a": {"b": "x"}}, _R, max_depth=1) == {"a": DEPTH_LIMIT_MARKER}
    assert redact_structure({"a": {"b": "S3cret"}}, _R, max_depth=2) == {"a": {"b": "[REDACTED]"}}
    assert redact_structure(["x"], _R, max_depth=0) == DEPTH_LIMIT_MARKER
    # ...and carried into list elements, not only dict values.
    assert redact_structure([["x"], "S3cret"], _R, max_depth=1) == [DEPTH_LIMIT_MARKER, "[REDACTED]"]
    assert redact_structure("S3cret", _R, max_depth=0) == "[REDACTED]"


def test_a_frame_far_deeper_than_the_recursion_limit_does_not_raise() -> None:
    depth = max(5000, sys.getrecursionlimit() + 100)
    out = redact_structure(_nest(depth, container="dict"), _R, key_aware=True)
    assert _innermost(out) == DEPTH_LIMIT_MARKER
    json.dumps(out)  # and the result is shallow enough to serialise


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "Token",
        "TOKEN",
        "resume_token",
        "resume-token",
        "Session-Token",
        "password",
        "passwd",
        "passphrase",
        "ssh_passphrase",
        "secret",
        "client_secret",
        "api_key",
        "api-key",
        "apikey",
        "openai_api_key",
        "authorization",
        "Authorization",
        "cookie",
        "private_key",
        "db_password",
        "accessToken",
        "clientSecret",
        "privateKey",
        "Set-Cookie",
        "Proxy-Authorization",
        "credentials",
        "credential",
        "aws_secret_access_key",
        "APIKey",
        "apiKey",
        "x-api-key",
        "access_key_id",
        "HTTPCookie",
        "refresh.token",
        "Session Token",
    ],
)
def test_sensitive_keys(key: str) -> None:
    assert is_sensitive_key(key) is True


@pytest.mark.parametrize(
    "key",
    [
        "",
        "_",
        "tokens",
        "token_count",
        "max_tokens",
        "screen",
        "keys",
        "passwords_seen",
        "secretary",
        "type",
        "tokenizer",
        "token_type",
        "password_required",
        "secret_hash",
        "cookie_len",
        "session_id",
        "api",
        "key",
        "keyApi",
        "privately_key",
        "TOKEN_TTL",
        "token2",
    ],
)
def test_ordinary_keys(key: str) -> None:
    assert is_sensitive_key(key) is False


def test_key_aware_masks_sensitive_values_whatever_they_look_like() -> None:
    frame = {"type": "resume", "token": "abc123", "player_id": 4}
    assert redact_structure(frame, _R, key_aware=True) == {
        "type": "resume",
        "token": SENSITIVE_KEY_MARKER,
        "player_id": 4,
    }
    nested = {"hello": {"resume_token": "tok", "caps": {"Password": 1, "secret": {"a": "b"}}}}
    assert redact_structure(nested, _R, key_aware=True) == {
        "hello": {
            "resume_token": SENSITIVE_KEY_MARKER,
            "caps": {"Password": SENSITIVE_KEY_MARKER, "secret": SENSITIVE_KEY_MARKER},
        }
    }


def test_key_aware_keeps_a_null_credential_null() -> None:
    assert redact_structure({"token": None}, _R, key_aware=True) == {"token": None}


def test_key_aware_is_off_by_default_and_propagates_when_on() -> None:
    frame = {"wrap": [{"token": "abc"}]}
    assert redact_structure(frame, _R) == {"wrap": [{"token": "abc"}]}
    assert redact_structure(frame, _R, key_aware=True) == {"wrap": [{"token": SENSITIVE_KEY_MARKER}]}


def test_non_string_keys_are_kept_and_never_matched() -> None:
    assert redact_structure({1: "S3cret", None: "v"}, _R, key_aware=True) == {1: "[REDACTED]", None: "v"}


@pytest.mark.parametrize(
    ("key", "segments"),
    [
        ("accessToken", ["access", "token"]),
        ("APIKey", ["api", "key"]),
        ("HTTPCookie", ["http", "cookie"]),
        ("RESUME_TOKEN", ["resume", "token"]),
        ("Resume-Token", ["resume", "token"]),
        ("a.b c-d__e", ["a", "b", "c", "d", "e"]),
        ("token2Fa", ["token2", "fa"]),
        ("X", ["x"]),
        ("aB", ["a", "b"]),
        ("AB", ["ab"]),
        ("ABc", ["a", "bc"]),
        ("ABC", ["abc"]),
        ("", []),
    ],
)
def test_key_segments(key: str, segments: list[str]) -> None:
    assert key_segments(key) == segments
