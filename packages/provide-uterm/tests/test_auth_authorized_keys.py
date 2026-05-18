#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for ``_parse_authorized_keys_line`` and the lower-level pubkey helpers.

Covers line parsing (options/keytype/payload/comment), ``_coerce_to_binary_pubkey``,
and ``fingerprint_from_openssh_blob`` — plus the surgical mutation-killers that
pin specific code-level decisions (return values, slice indices, prefix literals).
"""

from __future__ import annotations

import base64

import pytest

from provide.uterm.auth import (
    _coerce_to_binary_pubkey,
    _parse_authorized_keys_line,
    fingerprint_from_openssh_blob,
)

_ED25519_SAMPLE = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK7nKaxTKmzX0z3V6tGqmmOvkSiGXh3yF2J5vqkQTOY+"
)
_ED25519_KEYTYPE = "ssh-ed25519"
_ED25519_PAYLOAD_B64 = "AAAAC3NzaC1lZDI1NTE5AAAAIK7nKaxTKmzX0z3V6tGqmmOvkSiGXh3yF2J5vqkQTOY+"


# ---------------------------------------------------------------------------
# _parse_authorized_keys_line
# ---------------------------------------------------------------------------


def test_parse_authorized_keys_line_no_options_comment_becomes_subject() -> None:
    entry = _parse_authorized_keys_line(f"{_ED25519_SAMPLE} alice@laptop")
    assert entry.subject == "alice@laptop"
    assert entry.claims == {}
    assert entry.fingerprint.startswith("SHA256:")


def test_parse_authorized_keys_line_no_options_no_comment_falls_back_to_fingerprint() -> None:
    entry = _parse_authorized_keys_line(_ED25519_SAMPLE)
    assert entry.subject.startswith("key:SHA256:")
    assert entry.claims == {}


def test_parse_authorized_keys_line_subject_option_wins_over_comment() -> None:
    line = f'subject="bob",no-pty {_ED25519_SAMPLE} alice@laptop'
    entry = _parse_authorized_keys_line(line)
    assert entry.subject == "bob"
    # no-pty is a flag option — kept under _options.
    assert "_options" in entry.claims
    assert entry.claims["_options"] == {"no-pty": True}


def test_parse_authorized_keys_line_claim_prefix_options_become_claims() -> None:
    line = f'claim-team="ops",claim-role="oncall" {_ED25519_SAMPLE} c'
    entry = _parse_authorized_keys_line(line)
    assert entry.claims == {"team": "ops", "role": "oncall"}


def test_parse_authorized_keys_line_mixed_claim_and_non_claim_options() -> None:
    line = f'claim-team="ops",no-pty {_ED25519_SAMPLE} c'
    entry = _parse_authorized_keys_line(line)
    assert entry.claims["team"] == "ops"
    # The non-claim option is preserved under _options.
    assert entry.claims["_options"] == {"no-pty": True}


def test_parse_authorized_keys_line_empty_subject_option_falls_back_to_comment() -> None:
    # An explicit empty subject="" should NOT win — the parser must treat
    # it as not-set and fall back to the comment or fingerprint synthesis.
    line = f'subject="" {_ED25519_SAMPLE} alice@laptop'
    entry = _parse_authorized_keys_line(line)
    assert entry.subject == "alice@laptop"


def test_parse_authorized_keys_line_missing_payload_raises() -> None:
    with pytest.raises(ValueError, match="missing key payload"):
        _parse_authorized_keys_line("ssh-ed25519")


def test_parse_authorized_keys_line_keytype_detection_skips_options_when_first_token_is_keytype() -> None:
    # The first token starts with ``ssh-`` so it must be parsed as the
    # keytype, NOT as an options field.
    line = f"{_ED25519_SAMPLE} alice@laptop"
    entry = _parse_authorized_keys_line(line)
    assert entry.subject == "alice@laptop"
    # No "_options" key should appear when there are no options.
    assert "_options" not in entry.claims


def test_parse_authorized_keys_line_ecdsa_keytype_prefix_recognized() -> None:
    fake_ecdsa = "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTY="
    entry = _parse_authorized_keys_line(f"{fake_ecdsa} svc@oncall")
    assert entry.subject == "svc@oncall"


def test_parse_authorized_keys_line_sk_keytype_prefix_recognized() -> None:
    fake_sk = (
        "sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29tAAAAIA=="
    )
    entry = _parse_authorized_keys_line(f"{fake_sk} yubikey-prod")
    assert entry.subject == "yubikey-prod"


def test_parse_authorized_keys_line_uses_payload_token_for_fingerprint_not_comment() -> None:
    # ``blob_text = f"{keytype} {payload}"`` — the fingerprint must be
    # derived from keytype+payload, NOT keytype+payload+comment. Two
    # entries with the same key but different comments share a fp.
    line_a = f"{_ED25519_SAMPLE} alice"
    line_b = f"{_ED25519_SAMPLE} bob"
    entry_a = _parse_authorized_keys_line(line_a)
    entry_b = _parse_authorized_keys_line(line_b)
    assert entry_a.fingerprint == entry_b.fingerprint
    # But subjects differ.
    assert entry_a.subject != entry_b.subject


def test_parse_authorized_keys_line_options_field_with_comment_does_not_leak_into_payload() -> None:
    # When the first token is options, the second token must be the
    # keytype (not get absorbed into options). Pin by checking the
    # subject precedence works.
    line = f'subject="explicit" {_ED25519_SAMPLE} comment-ignored'
    entry = _parse_authorized_keys_line(line)
    assert entry.subject == "explicit"
    # The keytype hash must still validate — i.e. the parser found
    # the payload token correctly past the options.
    assert entry.fingerprint.startswith("SHA256:")


# ---------------------------------------------------------------------------
# _coerce_to_binary_pubkey + fingerprint_from_openssh_blob
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prefix",
    [b"ssh-", b"ecdsa-", b"sk-ssh-", b"sk-ecdsa-"],
)
def test_coerce_to_binary_pubkey_each_textual_prefix(prefix: bytes) -> None:
    # Build a minimal valid line for each prefix shape; only the first token
    # and the base64 payload matter for the decoder.
    payload = base64.b64encode(b"hello-bytes").decode("ascii")
    line = prefix + b"any " + payload.encode("ascii")
    out = _coerce_to_binary_pubkey(line)
    assert out == b"hello-bytes"


def test_coerce_to_binary_pubkey_text_form_missing_payload_raises() -> None:
    with pytest.raises(ValueError, match="malformed OpenSSH public key line"):
        _coerce_to_binary_pubkey(b"ssh-ed25519")


def test_coerce_to_binary_pubkey_invalid_base64_raises_with_helpful_message() -> None:
    with pytest.raises(ValueError, match="invalid base64 in public key"):
        _coerce_to_binary_pubkey(b"ssh-ed25519 !not-base64!")


def test_coerce_to_binary_pubkey_binary_form_passes_through_after_strip() -> None:
    # No recognised prefix — assume binary wire format; only .strip() applies.
    raw = b"  \x00\x00\x00\x0bssh-ed25519PAYLOAD  "
    assert _coerce_to_binary_pubkey(raw) == raw.strip()


def test_coerce_to_binary_pubkey_strip_removes_both_sides() -> None:
    # ``blob.strip()`` must trim BOTH leading and trailing whitespace —
    # mutmut may mutate to lstrip() or rstrip() only.
    payload = base64.b64encode(b"PAYLOAD").decode("ascii")
    leading = f"  ssh-ed25519 {payload}".encode("ascii")
    trailing = f"ssh-ed25519 {payload}  ".encode("ascii")
    both = f"  ssh-ed25519 {payload}  ".encode("ascii")
    assert _coerce_to_binary_pubkey(leading) == b"PAYLOAD"
    assert _coerce_to_binary_pubkey(trailing) == b"PAYLOAD"
    assert _coerce_to_binary_pubkey(both) == b"PAYLOAD"


def test_coerce_to_binary_pubkey_split_keeps_only_payload_token() -> None:
    # ``parts = stripped.split(None, 2)`` — we want index [1] (the
    # payload), not [0] (keytype) or [2] (comment).
    payload = base64.b64encode(b"P").decode("ascii")
    blob = f"ssh-ed25519 {payload} long comment with spaces".encode("ascii")
    assert _coerce_to_binary_pubkey(blob) == b"P"


def test_fingerprint_from_openssh_blob_text_and_binary_agree() -> None:
    text = f"{_ED25519_KEYTYPE} {_ED25519_PAYLOAD_B64}".encode("ascii")
    binary = base64.b64decode(_ED25519_PAYLOAD_B64)
    fp_text = fingerprint_from_openssh_blob(text)
    fp_bin = fingerprint_from_openssh_blob(binary)
    assert fp_text == fp_bin
    assert fp_text.startswith("SHA256:")


def test_fingerprint_format_omits_padding_equals() -> None:
    fp = fingerprint_from_openssh_blob(b"\x00\x00\x00\x0bssh-ed25519")
    # OpenSSH-style fingerprints strip the base64 '=' padding.
    assert "=" not in fp.split(":", 1)[1]


def test_fingerprint_uses_sha256_prefix_literally() -> None:
    # The prefix is the literal string "SHA256:". A mutation that swapped
    # the case ("Sha256:" / "sha256:") or character set must fail this.
    fp = fingerprint_from_openssh_blob(b"\x00\x00\x00\x0bssh-ed25519")
    assert fp.startswith("SHA256:")
    # Exact prefix length: 7 chars.
    assert fp.find(":") == 6
