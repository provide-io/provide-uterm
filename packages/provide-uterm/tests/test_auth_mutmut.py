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
    _coerce_to_binary_pubkey,
    _find_first_token_end,
    _parse_authorized_keys_line,
    _parse_options,
    _split_options,
    fingerprint_from_openssh_blob,
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

    def test_malformed_text_form_message_is_exact(self) -> None:
        # ``match=`` does a substring search, so a mutant that wraps the message
        # (``"XXmalformed OpenSSH public key lineXX"``) still matches. Pin the
        # EXACT message so the wrapper mutant is killed.
        with pytest.raises(ValueError) as exc_info:
            _coerce_to_binary_pubkey(b"ssh-ed25519")  # no payload token
        assert str(exc_info.value) == "malformed OpenSSH public key line"

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

    def test_missing_payload_message_is_exact(self) -> None:
        # ``match=`` is a substring search; a mutant wrapping the message
        # (``"XXmissing key payloadXX"``) would still satisfy it. Pin the
        # EXACT message to kill that wrapper mutant.
        with pytest.raises(ValueError) as exc_info:
            _parse_authorized_keys_line("ssh-ed25519")  # no base64
        assert str(exc_info.value) == "missing key payload"

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


# ---------------------------------------------------------------------------
# Documented EQUIVALENT mutants in ``auth.py``
#
# The following mutmut mutants produce byte-for-byte identical behaviour to the
# original — no test can distinguish them, so they are documented here (mirroring
# the existing equivalent-mutant skips in ``test_high_roi_mutmut.py``) rather than
# pinned with a bogus assertion. Each test states *why* the mutant is equivalent
# and exercises the code path so the reasoning is visible and regression-checked.
# ---------------------------------------------------------------------------


class TestEquivalentAuthMutants:
    def test_fingerprint_decode_codec_name_is_case_insensitive(self) -> None:
        """fingerprint_from_openssh_blob__mutmut_11: ``.decode("ascii")`` →
        ``.decode("ASCII")``.

        Python codec names are case-insensitive and normalised, so
        ``b.decode("ascii") == b.decode("ASCII")`` for every input. EQUIVALENT.
        """
        lower, upper = "ascii", "ASCII"
        assert b"abc".decode(lower) == b"abc".decode(upper)
        # Exercise the real function so the path stays covered.
        blob = b"ssh-ed25519 " + base64.b64encode(b"k")
        assert fingerprint_from_openssh_blob(blob).startswith("SHA256:")
        pytest.skip("equivalent mutant — codec name 'ascii'/'ASCII' resolve to the same codec")

    def test_fingerprint_rstrip_charset_superset_is_inert(self) -> None:
        """fingerprint_from_openssh_blob__mutmut_12: ``.rstrip("=")`` →
        ``.rstrip("XX=XX")`` (strip-set {'X', '='}).

        The value stripped is always ``base64(sha256(...))`` of a 32-byte digest:
        exactly 43 base64 chars + one '=' pad. The final (pre-padding) char
        encodes a 2-byte group, so its low 2 bits are always zero — it is always
        one of the 16 "low-2-bits-zero" base64 chars and never 'X' (whose low 2
        bits are ``11``). So stripping {'X','='} removes the same characters as
        stripping {'='}. EQUIVALENT.
        """
        low2_zero = {
            c for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/") if i % 4 == 0
        }
        last_chars = set()
        for i in range(2000):
            digest = hashlib.sha256(str(i).encode()).digest()
            full = base64.b64encode(digest).decode("ascii")
            last_chars.add(full.rstrip("=")[-1])
        # The trailing (pre-padding) base64 char of a 32-byte digest has low 2 bits = 0.
        assert last_chars <= low2_zero
        assert "X" not in last_chars
        pytest.skip("equivalent mutant — sha256 digest base64 never ends in 'X' before padding")

    def test_coerce_split_limit_does_not_affect_payload_token(self) -> None:
        """_coerce_to_binary_pubkey__mutmut_14 (``split(None,2)`` → ``split(None)``)
        and __mutmut_16 (``→ split(None, 3)``).

        Only ``parts[1]`` (the base64 payload) is ever read. ``parts[1]`` is the
        second whitespace-delimited token regardless of any maxsplit ≥ 1, so the
        limit is irrelevant here. EQUIVALENT. (Mirrors the existing skips for the
        same mutants in test_high_roi_mutmut.py.)
        """
        blob = b"ssh-ed25519 " + base64.b64encode(b"PAY") + b" word1 word2 word3"
        assert _coerce_to_binary_pubkey(blob) == b"PAY"
        pytest.skip("equivalent mutant — split-limit doesn't affect parts[1]")

    def test_load_entries_encoding_none_equals_utf8_on_utf8_locale(self) -> None:
        """_load_entries__mutmut_3: ``read_text(encoding="utf-8")`` →
        ``read_text(encoding=None)``.

        With ``encoding=None``, Path.read_text uses the process default encoding
        (``locale.getpreferredencoding(False)``), which is UTF-8 on CI and every
        supported platform. So the two reads are identical there. EQUIVALENT in
        the supported environment.
        """
        import locale

        assert locale.getpreferredencoding(False).lower().replace("-", "") == "utf8"
        pytest.skip("equivalent mutant — encoding=None resolves to UTF-8 on the supported (UTF-8 locale) platform")

    def test_load_entries_encoding_codec_name_is_case_insensitive(self) -> None:
        """_load_entries__mutmut_5: ``encoding="utf-8"`` → ``encoding="UTF-8"``.

        Codec names are case-insensitive; both name the same UTF-8 codec.
        EQUIVALENT.
        """
        lower, upper = "utf-8", "UTF-8"
        assert "café".encode(lower) == "café".encode(upper)
        pytest.skip("equivalent mutant — codec name 'utf-8'/'UTF-8' resolve to the same codec")

    def test_parse_line_empty_vs_none_options_str_is_falsy_either_way(self) -> None:
        """_parse_authorized_keys_line__mutmut_14: in the keytype branch,
        ``options_str = ""`` → ``options_str = None``.

        In that branch ``options_str`` is only consumed by
        ``opts = _parse_options(options_str) if options_str else {}``. Both ``""``
        and ``None`` are falsy → ``opts = {}``; the value is read nowhere else.
        EQUIVALENT.
        """
        entry = _parse_authorized_keys_line(f"ssh-ed25519 {base64.b64encode(b'k').decode()} who")
        assert entry.subject == "who"
        assert entry.claims == {}
        pytest.skip("equivalent mutant — '' and None are both falsy and options_str is otherwise unused here")

    def test_parse_line_lstrip_vs_rstrip_only_touches_discarded_whitespace(self) -> None:
        """_parse_authorized_keys_line__mutmut_19: in the options branch,
        ``line[first_token_end:].lstrip()`` → ``.rstrip()``.

        The slice always begins with the whitespace token-boundary, and
        ``str.split(None, ...)`` ignores leading/trailing whitespace, so the
        keytype/payload parse is unaffected. The only difference is trailing
        whitespace inside ``parts[2]`` (the comment), which is then ``.strip()``-ed
        into the subject and stored nowhere raw. EQUIVALENT.
        """
        line = f'subject="x" ssh-ed25519 {base64.b64encode(b"k").decode()} multi word comment   '
        entry = _parse_authorized_keys_line(line)
        assert entry.subject == "x"
        pytest.skip("equivalent mutant — lstrip/rstrip differ only on whitespace that is later discarded")

    def test_parse_line_encode_codec_name_is_case_insensitive(self) -> None:
        """_parse_authorized_keys_line__mutmut_44: ``.encode("ascii")`` →
        ``.encode("ASCII")``.

        Codec names are case-insensitive; both encode to identical bytes.
        EQUIVALENT.
        """
        lower, upper = "ascii", "ASCII"
        assert "ssh-ed25519 AAAA".encode(lower) == "ssh-ed25519 AAAA".encode(upper)
        pytest.skip("equivalent mutant — codec name 'ascii'/'ASCII' resolve to the same codec")

    def test_find_first_token_end_none_init_behaves_as_false(self) -> None:
        """_find_first_token_end__mutmut_1: ``in_quotes = False`` →
        ``in_quotes = None``.

        ``in_quotes`` is only read in boolean context (``not in_quotes``) and
        reassigned via ``not in_quotes``. ``not None == not False == True`` and
        the first ``"`` toggles it to a real bool, so the truthiness sequence is
        identical for every input. EQUIVALENT.
        """
        assert _find_first_token_end('command="echo hi" rest') == len('command="echo hi"')
        assert _find_first_token_end(" rest") == 0
        pytest.skip("equivalent mutant — None initial value is falsy exactly like False here")

    def test_split_options_none_init_behaves_as_false(self) -> None:
        """_split_options__mutmut_3: ``in_quotes = False`` → ``in_quotes = None``.

        Same reasoning as _find_first_token_end__mutmut_1: ``in_quotes`` is only
        used as a boolean and reassigned via ``not in_quotes``; ``None`` is falsy
        exactly like ``False``. EQUIVALENT.
        """
        assert _split_options('"a,b","c,d"') == ['"a,b"', '"c,d"']
        pytest.skip("equivalent mutant — None initial value is falsy exactly like False here")
