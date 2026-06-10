#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for control_channel_builders — typed builder functions for the ControlChannel protocol."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from provide.uterm.control_channel import ControlChannelDecoder, encode_control
from provide.uterm.control_channel_builders import (
    _canonical_identity_signature_payload,
    make_identity,
    make_link_patterns,
    make_presence_update,
    make_resume,
    make_resume_failed,
    make_resume_ok,
    make_session_token,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _round_trip(payload: dict) -> dict:
    """Encode *payload* via encode_control, decode via ControlChannelDecoder, return the control dict."""
    decoder = ControlChannelDecoder()
    encoded = encode_control(payload)
    chunks = decoder.feed(encoded)
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.kind == "control"
    return chunk.control  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# make_identity
# ---------------------------------------------------------------------------


class TestMakeIdentity:
    def test_default_identity_shape_exact(self) -> None:
        assert make_identity("user:alice") == {
            "type": "identity",
            "version": 1,
            "subject": "user:alice",
            "fingerprint": "",
            "transport": "ssh",
        }

    def test_happy_path_minimal(self) -> None:
        msg = make_identity("user:alice")
        assert msg["type"] == "identity"
        assert msg["version"] == 1
        assert msg["subject"] == "user:alice"
        assert msg["fingerprint"] == ""
        assert msg["transport"] == "ssh"
        assert "claims" not in msg

    def test_happy_path_full(self) -> None:
        msg = make_identity(
            "user:bob",
            claims={"role": "admin", "org": "acme"},
            fingerprint="SHA256:abc123",
            transport="ws",
        )
        assert msg["type"] == "identity"
        assert msg["subject"] == "user:bob"
        assert msg["claims"] == {"role": "admin", "org": "acme"}
        assert msg["fingerprint"] == "SHA256:abc123"
        assert msg["transport"] == "ws"

    def test_claims_are_copied(self) -> None:
        original = {"role": "user"}
        msg = make_identity("user:x", claims=original)
        msg["claims"]["extra"] = "injected"
        assert "extra" not in original, "claims dict must be a fresh copy"

    def test_empty_subject_raises(self) -> None:
        with pytest.raises(ValueError, match="subject"):
            make_identity("")

    def test_empty_subject_error_message_exact(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            make_identity("")

        assert str(exc_info.value) == "make_identity: 'subject' must be a non-empty string"

    def test_returns_fresh_dict_each_call(self) -> None:
        a = make_identity("user:x")
        b = make_identity("user:x")
        assert a is not b

    def test_round_trip(self) -> None:
        payload = make_identity("user:alice", claims={"role": "admin"}, fingerprint="fp1")
        recovered = _round_trip(payload)
        assert recovered["type"] == "identity"
        assert recovered["subject"] == "user:alice"
        assert recovered["claims"] == {"role": "admin"}

    def test_canonical_signature_payload_exact(self) -> None:
        payload = _canonical_identity_signature_payload(
            version=1,
            subject="user:alice",
            fingerprint="SHA256:abc",
            transport="ssh",
            claims={"scope": ["write", "read"], "role": "admin"},
        )

        assert payload == b'1:user:alice:SHA256:abc:ssh:{"role":"admin","scope":["write","read"]}'

    def test_signature_string_secret_exact(self) -> None:
        msg = make_identity(
            "user:alice",
            claims={"scope": ["write", "read"], "role": "admin"},
            fingerprint="SHA256:abc",
            transport="ws",
            secret="proxy-secret",  # pragma: allowlist secret
        )
        expected_payload = b'1:user:alice:SHA256:abc:ws:{"role":"admin","scope":["write","read"]}'
        expected_signature = hmac.new(b"proxy-secret", expected_payload, hashlib.sha256).hexdigest()

        assert msg == {
            "type": "identity",
            "version": 1,
            "subject": "user:alice",
            "fingerprint": "SHA256:abc",
            "transport": "ws",
            "claims": {"scope": ["write", "read"], "role": "admin"},
            "signature": expected_signature,
        }

    def test_signature_bytes_secret_matches_string_secret(self) -> None:
        as_text = make_identity("user:alice", fingerprint="fp", secret="proxy-secret")
        as_bytes = make_identity("user:alice", fingerprint="fp", secret=b"proxy-secret")

        assert as_bytes["signature"] == as_text["signature"]

    def test_signature_without_claims_uses_empty_claims_payload(self) -> None:
        msg = make_identity("user:alice", fingerprint="fp", transport="ssh", secret="proxy-secret")
        expected_payload = b"1:user:alice:fp:ssh:{}"
        expected_signature = hmac.new(b"proxy-secret", expected_payload, hashlib.sha256).hexdigest()

        assert "claims" not in msg
        assert msg["signature"] == expected_signature

    def test_empty_secret_does_not_sign(self) -> None:
        msg = make_identity("user:alice", secret="")

        assert "signature" not in msg


# ---------------------------------------------------------------------------
# make_session_token
# ---------------------------------------------------------------------------


class TestMakeSessionToken:
    def test_happy_path_minimal(self) -> None:
        msg = make_session_token("tok-abc")
        assert msg["type"] == "session_token"
        assert msg["token"] == "tok-abc"
        assert "player_id" not in msg

    def test_happy_path_with_player_id(self) -> None:
        msg = make_session_token("tok-xyz", player_id=42)
        assert msg["type"] == "session_token"
        assert msg["token"] == "tok-xyz"
        assert msg["player_id"] == 42

    def test_empty_token_raises(self) -> None:
        with pytest.raises(ValueError, match="token"):
            make_session_token("")

    def test_empty_token_error_message_exact(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            make_session_token("")

        assert str(exc_info.value) == "make_session_token: 'token' must be a non-empty string"

    def test_player_id_zero_is_included(self) -> None:
        msg = make_session_token("tok", player_id=0)
        assert msg["player_id"] == 0

    def test_round_trip(self) -> None:
        payload = make_session_token("tok-rt", player_id=7)
        recovered = _round_trip(payload)
        assert recovered["type"] == "session_token"
        assert recovered["token"] == "tok-rt"
        assert recovered["player_id"] == 7


# ---------------------------------------------------------------------------
# make_resume
# ---------------------------------------------------------------------------


class TestMakeResume:
    def test_happy_path_minimal(self) -> None:
        msg = make_resume("resume-tok")
        assert msg["type"] == "resume"
        assert msg["token"] == "resume-tok"
        assert "player_id" not in msg

    def test_happy_path_with_player_id(self) -> None:
        msg = make_resume("resume-tok", player_id=15)
        assert msg["player_id"] == 15

    def test_empty_token_raises(self) -> None:
        with pytest.raises(ValueError, match="token"):
            make_resume("")

    def test_empty_token_error_message_exact(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            make_resume("")

        assert str(exc_info.value) == "make_resume: 'token' must be a non-empty string"

    def test_round_trip(self) -> None:
        payload = make_resume("resume-rt")
        recovered = _round_trip(payload)
        assert recovered["type"] == "resume"
        assert recovered["token"] == "resume-rt"


# ---------------------------------------------------------------------------
# make_resume_ok
# ---------------------------------------------------------------------------


class TestMakeResumeOk:
    def test_type_field(self) -> None:
        msg = make_resume_ok()
        assert msg == {"type": "resume_ok"}

    def test_no_none_fields_are_emitted(self) -> None:
        assert None not in make_resume_ok().values()

    def test_returns_fresh_dict(self) -> None:
        a = make_resume_ok()
        b = make_resume_ok()
        assert a is not b

    def test_round_trip(self) -> None:
        recovered = _round_trip(make_resume_ok())
        assert recovered == {"type": "resume_ok"}


# ---------------------------------------------------------------------------
# make_resume_failed
# ---------------------------------------------------------------------------


class TestMakeResumeFailed:
    def test_happy_path_no_reason(self) -> None:
        msg = make_resume_failed()
        assert msg["type"] == "resume_failed"
        assert "reason" not in msg

    def test_happy_path_with_reason(self) -> None:
        msg = make_resume_failed(reason="token expired")
        assert msg["type"] == "resume_failed"
        assert msg["reason"] == "token expired"

    def test_empty_string_reason_is_included(self) -> None:
        msg = make_resume_failed(reason="")
        assert "reason" in msg
        assert msg["reason"] == ""

    def test_returns_fresh_dict(self) -> None:
        a = make_resume_failed()
        b = make_resume_failed()
        assert a is not b


# ---------------------------------------------------------------------------
# make_link_patterns
# ---------------------------------------------------------------------------


class TestMakeLinkPatterns:
    def test_single_pattern_round_trip(self) -> None:
        msg = make_link_patterns([{"pattern": r"\bsector\b", "action": "cmd"}])
        assert msg["type"] == "link_patterns"
        assert len(msg["patterns"]) == 1
        assert msg["patterns"][0]["pattern"] == r"\bsector\b"
        assert msg["patterns"][0]["action"] == "cmd"

    def test_multiple_patterns_preserved_in_order(self) -> None:
        entries = [
            {"pattern": "alpha", "action": "cmd"},
            {"pattern": "beta", "action": "url"},
            {"pattern": "gamma", "action": "key"},
        ]
        msg = make_link_patterns(entries)
        patterns = msg["patterns"]
        assert [p["pattern"] for p in patterns] == ["alpha", "beta", "gamma"]
        assert [p["action"] for p in patterns] == ["cmd", "url", "key"]

    def test_all_valid_actions(self) -> None:
        for action in ("cmd", "url", "key", "focus"):
            msg = make_link_patterns([{"pattern": "x", "action": action}])
            assert msg["patterns"][0]["action"] == action

    def test_missing_pattern_field_raises(self) -> None:
        with pytest.raises(ValueError, match="pattern"):
            make_link_patterns([{"action": "cmd"}])

    def test_missing_action_field_raises(self) -> None:
        with pytest.raises(ValueError, match="action"):
            make_link_patterns([{"pattern": "x"}])

    def test_invalid_action_raises(self) -> None:
        with pytest.raises(ValueError, match=r"entry\[0\]"):
            make_link_patterns([{"pattern": "x", "action": "teleport"}])

    def test_invalid_action_error_mentions_index_and_valid_choices(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            make_link_patterns([{"pattern": "x", "action": "bad"}])

        message = str(exc_info.value)
        assert "entry[0]" in message
        for choice in ("cmd", "url", "key", "focus"):
            assert choice in message

    def test_error_mentions_entry_index(self) -> None:
        with pytest.raises(ValueError, match=r"entry\[1\]"):
            make_link_patterns(
                [
                    {"pattern": "good", "action": "cmd"},
                    {"action": "cmd"},  # missing pattern at index 1
                ]
            )

    def test_all_optional_fields_populated(self) -> None:
        entry = {
            "pattern": r"\d+",
            "action": "url",
            "id": "p.num",
            "flags": "gi",
            "group": 1,
            "payload": "https://example.com/",
            "hover": "Open link",
            "class": "external-link",
        }
        msg = make_link_patterns([entry])
        p = msg["patterns"][0]
        assert p["id"] == "p.num"
        assert p["flags"] == "gi"
        assert p["group"] == 1
        assert p["payload"] == "https://example.com/"
        assert p["hover"] == "Open link"
        assert p["class"] == "external-link"

    def test_optional_fields_absent_when_not_given(self) -> None:
        msg = make_link_patterns([{"pattern": "x", "action": "key"}])
        p = msg["patterns"][0]
        for key in ("id", "flags", "group", "payload", "hover", "class"):
            assert key not in p, f"unexpected key {key!r} in output"

    def test_empty_patterns_list(self) -> None:
        msg = make_link_patterns([])
        assert msg["type"] == "link_patterns"
        assert msg["patterns"] == []

    def test_round_trip_encode_decode(self) -> None:
        payload = make_link_patterns([{"pattern": "foo", "action": "focus"}])
        recovered = _round_trip(payload)
        assert recovered["type"] == "link_patterns"
        assert recovered["patterns"][0]["pattern"] == "foo"
        assert recovered["patterns"][0]["action"] == "focus"

    def test_input_mapping_not_mutated(self) -> None:
        """Ensure we don't modify caller's original entry dict."""
        entry: dict = {"pattern": "x", "action": "cmd"}
        make_link_patterns([entry])
        assert set(entry.keys()) == {"pattern", "action"}

    def test_line_contains_preserved(self) -> None:
        """``line_contains`` scopes a pattern to matching lines; it must survive."""
        msg = make_link_patterns([{"pattern": r"\((\d+)\)", "action": "cmd", "line_contains": "Warps to Sector"}])
        assert msg["patterns"][0]["line_contains"] == "Warps to Sector"

    def test_line_contains_absent_when_not_given(self) -> None:
        msg = make_link_patterns([{"pattern": "x", "action": "cmd"}])
        assert "line_contains" not in msg["patterns"][0]

    def test_unknown_field_raises_instead_of_silent_drop(self) -> None:
        """An unmodelled field must fail loud, not vanish at the wire."""
        with pytest.raises(ValueError, match=r"entry\[0\]"):
            make_link_patterns([{"pattern": "x", "action": "cmd", "bogus": 1}])


# ---------------------------------------------------------------------------
# make_presence_update
# ---------------------------------------------------------------------------


class TestMakePresenceUpdate:
    def test_happy_path_minimal(self) -> None:
        msg = make_presence_update("u1")
        assert msg["type"] == "presence_update"
        assert msg["user_id"] == "u1"

    def test_none_fields_are_omitted(self) -> None:
        msg = make_presence_update("u1", scroll_line=None)
        assert msg == {"type": "presence_update", "user_id": "u1"}

    def test_happy_path_with_extra_fields(self) -> None:
        msg = make_presence_update("u2", scroll_line=42, cursor_col=10)
        assert msg["type"] == "presence_update"
        assert msg["user_id"] == "u2"
        assert msg["scroll_line"] == 42
        assert msg["cursor_col"] == 10

    def test_returns_fresh_dict(self) -> None:
        a = make_presence_update("u1")
        b = make_presence_update("u1")
        assert a is not b

    def test_round_trip(self) -> None:
        payload = make_presence_update("u3", scroll_line=5)
        recovered = _round_trip(payload)
        assert recovered["type"] == "presence_update"
        assert recovered["user_id"] == "u3"
        assert recovered["scroll_line"] == 5
