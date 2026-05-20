#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Surgical mutmut-killer tests for ``provide.uterm.auth``.

Targets remaining mutations in:
- ``_parse_authorized_keys_line`` (keytype prefix tuple, options/keytype branching)
- ``_coerce_to_binary_pubkey`` (text/binary detection, error path)
- ``fingerprint_from_openssh_blob`` (SHA256 prefix + digest format)
- ``AuthorizedKeysFileResolver._load_entries`` (skip rules)
- ``_split_options`` / ``_parse_options`` / ``_find_first_token_end``
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from provide.uterm.auth import (
    AuthorizedKeysFileResolver,
    fingerprint_from_openssh_blob,
)
from provide.uterm.auth import (
    _coerce_to_binary_pubkey,
    _find_first_token_end,
    _parse_authorized_keys_line,
    _parse_options,
    _split_options,
)


# ---------------------------------------------------------------------------
# fingerprint_from_openssh_blob — SHA256 prefix + format
# ---------------------------------------------------------------------------


class TestFingerprintFormat:
    def test_starts_with_sha256_prefix(self) -> None:
        blob = b"ssh-ed25519 " + base64.b64encode(b"hello-key-bytes")
        fp = fingerprint_from_openssh_blob(blob)
        assert fp.startswith("SHA256:"), f"missing SHA256: prefix: {fp!r}"

    def test_digest_matches_manual_sha256(self) -> None:
        payload = b"hello-key-bytes"
        blob = b"ssh-ed25519 " + base64.b64encode(payload)
        fp = fingerprint_from_openssh_blob(blob)
        expected = base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii").rstrip("=")
        assert fp == f"SHA256:{expected}"

    def test_fingerprint_excludes_padding_equals(self) -> None:
        """``rstrip('=')`` strips base64 padding from the fingerprint."""
        blob = b"ssh-ed25519 " + base64.b64encode(b"x")
        fp = fingerprint_from_openssh_blob(blob)
        # Past the "SHA256:" prefix there must be no trailing '='.
        assert not fp.endswith("=")


# ---------------------------------------------------------------------------
# _coerce_to_binary_pubkey — text vs binary detection
# ---------------------------------------------------------------------------


class TestCoerceToBinaryPubkey:
    def test_ssh_dash_prefix_decoded_as_text(self) -> None:
        decoded = _coerce_to_binary_pubkey(b"ssh-ed25519 " + base64.b64encode(b"key"))
        assert decoded == b"key"

    def test_ecdsa_dash_prefix_decoded_as_text(self) -> None:
        decoded = _coerce_to_binary_pubkey(b"ecdsa-sha2-nistp256 " + base64.b64encode(b"key"))
        assert decoded == b"key"

    def test_sk_ssh_dash_prefix_decoded_as_text(self) -> None:
        decoded = _coerce_to_binary_pubkey(b"sk-ssh-ed25519 " + base64.b64encode(b"key"))
        assert decoded == b"key"

    def test_sk_ecdsa_dash_prefix_decoded_as_text(self) -> None:
        decoded = _coerce_to_binary_pubkey(b"sk-ecdsa-sha2-nistp256 " + base64.b64encode(b"key"))
        assert decoded == b"key"

    def test_unrecognised_prefix_passes_through_unchanged(self) -> None:
        raw = b"\x00\x00\x00\x0bssh-ed25519binarykeydata"
        assert _coerce_to_binary_pubkey(raw) == raw

    def test_malformed_text_form_raises_valueerror(self) -> None:
        with pytest.raises(ValueError, match="malformed OpenSSH public key"):
            _coerce_to_binary_pubkey(b"ssh-ed25519")  # no payload token

    def test_invalid_base64_raises_valueerror(self) -> None:
        with pytest.raises(ValueError, match="invalid base64"):
            _coerce_to_binary_pubkey(b"ssh-ed25519 not!!base64!!")

    def test_strips_whitespace_before_prefix_check(self) -> None:
        decoded = _coerce_to_binary_pubkey(b"  ssh-ed25519 " + base64.b64encode(b"k") + b" \n")
        assert decoded == b"k"


# ---------------------------------------------------------------------------
# _parse_authorized_keys_line — branching + options vs no-options
# ---------------------------------------------------------------------------


class TestParseAuthorizedKeysLine:
    @staticmethod
    def _line(keytype: str, payload: bytes, *, options: str = "", comment: str = "") -> str:
        b64 = base64.b64encode(payload).decode("ascii")
        parts = [s for s in [options, keytype, b64, comment] if s]
        return " ".join(parts)

    def test_ssh_ed25519_line_without_options(self) -> None:
        entry = _parse_authorized_keys_line(self._line("ssh-ed25519", b"keydata"))
        assert entry.fingerprint.startswith("SHA256:")
        assert entry.subject.startswith("key:SHA256:")  # default subject

    def test_ecdsa_line_without_options(self) -> None:
        entry = _parse_authorized_keys_line(self._line("ecdsa-sha2-nistp256", b"keydata"))
        assert entry.fingerprint.startswith("SHA256:")

    def test_sk_ssh_line_without_options(self) -> None:
        entry = _parse_authorized_keys_line(self._line("sk-ssh-ed25519", b"keydata"))
        assert entry.fingerprint.startswith("SHA256:")

    def test_sk_ecdsa_line_without_options(self) -> None:
        entry = _parse_authorized_keys_line(self._line("sk-ecdsa-sha2-nistp256", b"keydata"))
        assert entry.fingerprint.startswith("SHA256:")

    def test_comment_becomes_subject_when_no_subject_option(self) -> None:
        entry = _parse_authorized_keys_line(self._line("ssh-ed25519", b"k", comment="alice@host"))
        assert entry.subject == "alice@host"

    def test_subject_option_overrides_comment(self) -> None:
        line = f'subject="user:alice" ssh-ed25519 {base64.b64encode(b"k").decode()} comment-text'
        entry = _parse_authorized_keys_line(line)
        assert entry.subject == "user:alice"

    def test_claim_options_become_claims_dict(self) -> None:
        line = f'claim-role="oncall",claim-display="alice" ssh-ed25519 {base64.b64encode(b"k").decode()}'
        entry = _parse_authorized_keys_line(line)
        assert entry.claims["role"] == "oncall"
        assert entry.claims["display"] == "alice"

    def test_non_claim_options_collected_under_underscore_options(self) -> None:
        line = f'no-pty,command="echo hi" ssh-ed25519 {base64.b64encode(b"k").decode()}'
        entry = _parse_authorized_keys_line(line)
        assert "_options" in entry.claims
        opts = entry.claims["_options"]
        assert opts["no-pty"] is True
        assert opts["command"] == "echo hi"

    def test_missing_payload_raises(self) -> None:
        with pytest.raises(ValueError, match="missing key payload"):
            _parse_authorized_keys_line("ssh-ed25519")  # no base64

    def test_default_subject_when_neither_option_nor_comment(self) -> None:
        entry = _parse_authorized_keys_line(self._line("ssh-ed25519", b"k"))
        assert entry.subject.startswith("key:SHA256:")
        # Subject body matches the computed fingerprint.
        assert entry.subject == f"key:{entry.fingerprint}"


# ---------------------------------------------------------------------------
# _split_options + _parse_options + _find_first_token_end
# ---------------------------------------------------------------------------


class TestOptionHelpers:
    def test_split_options_respects_quoted_commas(self) -> None:
        result = _split_options('command="ls,cat,grep",no-pty')
        # Inside quotes, commas are part of the value.
        assert result == ['command="ls,cat,grep"', "no-pty"]

    def test_split_options_empty_input(self) -> None:
        assert _split_options("") == []

    def test_parse_options_assigns_value_pairs(self) -> None:
        out = _parse_options('key="value"')
        assert out == {"key": "value"}

    def test_parse_options_treats_bare_token_as_flag_true(self) -> None:
        out = _parse_options("no-pty")
        assert out == {"no-pty": True}

    def test_parse_options_strips_quotes_from_value(self) -> None:
        out = _parse_options('subject="u:a"')
        assert out["subject"] == "u:a"

    def test_find_first_token_end_returns_index_of_first_whitespace(self) -> None:
        assert _find_first_token_end("abc def") == 3

    def test_find_first_token_end_respects_quotes(self) -> None:
        # The space inside quotes is NOT counted as the token boundary.
        line = 'command="echo hi" ssh-ed25519 AAAA'
        idx = _find_first_token_end(line)
        # The first unquoted whitespace is at the end of "command=...".
        assert line[:idx] == 'command="echo hi"'

    def test_find_first_token_end_returns_full_length_when_no_whitespace(self) -> None:
        assert _find_first_token_end("no-spaces") == len("no-spaces")


# ---------------------------------------------------------------------------
# AuthorizedKeysFileResolver._load_entries — skip rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAuthorizedKeysFileResolverLoad:
    async def test_missing_file_returns_empty(self, tmp_path: pytest.TempPathFactory) -> None:
        path = tmp_path / "no-such-file"  # type: ignore[attr-defined]
        resolver = AuthorizedKeysFileResolver(path)
        result = await resolver.resolve("SHA256:anything", pubkey_blob=b"", username="")
        assert result is None

    async def test_blank_lines_skipped(self, tmp_path: pytest.TempPathFactory) -> None:
        path = tmp_path / "ak"  # type: ignore[attr-defined]
        path.write_text(  # type: ignore[union-attr]
            "\n   \n# a comment\nssh-ed25519 " + base64.b64encode(b"k").decode() + " alice\n"
        )
        resolver = AuthorizedKeysFileResolver(path)
        # Compute the actual fingerprint of the line so we can resolve it.
        fp = fingerprint_from_openssh_blob(f"ssh-ed25519 {base64.b64encode(b'k').decode()}".encode())
        identity = await resolver.resolve(fp, pubkey_blob=b"", username="alice")
        assert identity is not None
        assert identity.subject == "alice"

    async def test_comment_lines_skipped(self, tmp_path: pytest.TempPathFactory) -> None:
        path = tmp_path / "ak"  # type: ignore[attr-defined]
        path.write_text("# only-a-comment\n")  # type: ignore[union-attr]
        resolver = AuthorizedKeysFileResolver(path)
        result = await resolver.resolve("SHA256:any", pubkey_blob=b"", username="")
        assert result is None

    async def test_malformed_lines_skipped_not_abort(self, tmp_path: pytest.TempPathFactory) -> None:
        """One bad line should not lock out other valid keys."""
        path = tmp_path / "ak"  # type: ignore[attr-defined]
        good = f"ssh-ed25519 {base64.b64encode(b'k').decode()} bob"
        path.write_text(f"garbage line no payload\n{good}\n")  # type: ignore[union-attr]
        resolver = AuthorizedKeysFileResolver(path)
        fp = fingerprint_from_openssh_blob(f"ssh-ed25519 {base64.b64encode(b'k').decode()}".encode())
        identity = await resolver.resolve(fp, pubkey_blob=b"", username="bob")
        assert identity is not None
        assert identity.subject == "bob"
