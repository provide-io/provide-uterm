#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.auth — the pubkey-resolver boundary.

Covers the Protocol, the NullResolver no-op, the AuthorizedKeysFileResolver
parser, and the fingerprint helper. Gateway integration is tested separately
in the provide-uterm-server test tree.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from provide.terminal.auth import (
    AuthorizedKeysFileResolver,
    NullResolver,
    ResolvedIdentity,
    SSHKeyResolver,
    fingerprint_from_openssh_blob,
)

# Sample ed25519 public key (base64-decoded bytes below). Picked from an
# ed25519 keypair generated once and stored inline so tests don't depend
# on shelling out to ssh-keygen.
_SAMPLE_ED25519_OPENSSH = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK7nKaxTKmzX0z3V6tGqmmOvkSiGXh3yF2J5vqkQTOY+ alice@laptop"
)


def _compute_expected_fingerprint(openssh_line: str) -> str:
    """Independent recomputation of the fingerprint for cross-check."""
    import base64

    payload_b64 = openssh_line.split()[1]
    binary = base64.b64decode(payload_b64)
    digest = hashlib.sha256(binary).digest()
    b64 = base64.b64encode(digest).decode("ascii").rstrip("=")
    return f"SHA256:{b64}"


class TestResolvedIdentity:
    def test_defaults(self) -> None:
        r = ResolvedIdentity(subject="sre:alice")
        assert r.subject == "sre:alice"
        assert r.claims == {}
        assert r.fingerprint == ""

    def test_with_claims(self) -> None:
        r = ResolvedIdentity(
            subject="player:42",
            claims={"role": "oncall", "theme": "dark"},
            fingerprint="SHA256:abc",
        )
        assert r.claims["role"] == "oncall"
        assert r.fingerprint == "SHA256:abc"

    def test_frozen(self) -> None:
        r = ResolvedIdentity(subject="x")
        with pytest.raises(Exception):  # FrozenInstanceError
            r.subject = "y"  # type: ignore[misc]


class TestSSHKeyResolverProtocol:
    def test_null_resolver_satisfies_protocol(self) -> None:
        """runtime_checkable Protocol → isinstance check passes for NullResolver."""
        assert isinstance(NullResolver(), SSHKeyResolver)

    def test_file_resolver_satisfies_protocol(self, tmp_path: Path) -> None:
        assert isinstance(AuthorizedKeysFileResolver(tmp_path / "x"), SSHKeyResolver)


@pytest.mark.asyncio
class TestNullResolver:
    async def test_always_returns_none(self) -> None:
        r = NullResolver()
        result = await r.resolve(
            "SHA256:whatever",
            pubkey_blob=b"ssh-ed25519 AAAAC3...",
            username="anyone",
        )
        assert result is None


class TestFingerprintHelper:
    def test_fingerprint_from_text_form(self) -> None:
        expected = _compute_expected_fingerprint(_SAMPLE_ED25519_OPENSSH)
        got = fingerprint_from_openssh_blob(_SAMPLE_ED25519_OPENSSH.encode("ascii"))
        assert got == expected
        assert got.startswith("SHA256:")

    def test_fingerprint_from_binary_form(self) -> None:
        import base64

        payload_b64 = _SAMPLE_ED25519_OPENSSH.split()[1]
        binary = base64.b64decode(payload_b64)
        got = fingerprint_from_openssh_blob(binary)
        expected = _compute_expected_fingerprint(_SAMPLE_ED25519_OPENSSH)
        assert got == expected

    def test_malformed_raises(self) -> None:
        with pytest.raises(ValueError):
            fingerprint_from_openssh_blob(b"ssh-ed25519")  # no payload


@pytest.mark.asyncio
class TestAuthorizedKeysFileResolver:
    async def test_empty_file_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "authorized_keys"
        path.write_text("", encoding="utf-8")
        resolver = AuthorizedKeysFileResolver(path)
        result = await resolver.resolve(
            "SHA256:any",
            pubkey_blob=b"",
            username="x",
        )
        assert result is None

    async def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        resolver = AuthorizedKeysFileResolver(tmp_path / "nope")
        result = await resolver.resolve(
            "SHA256:any",
            pubkey_blob=b"",
            username="x",
        )
        assert result is None

    async def test_comment_becomes_subject_when_no_options(self, tmp_path: Path) -> None:
        path = tmp_path / "authorized_keys"
        path.write_text(_SAMPLE_ED25519_OPENSSH + "\n", encoding="utf-8")
        resolver = AuthorizedKeysFileResolver(path)

        fp = _compute_expected_fingerprint(_SAMPLE_ED25519_OPENSSH)
        result = await resolver.resolve(fp, pubkey_blob=b"", username="alice")
        assert result is not None
        assert result.subject == "alice@laptop"
        assert result.fingerprint == fp
        assert result.claims == {}

    async def test_explicit_subject_and_claims(self, tmp_path: Path) -> None:
        line = (
            'subject="sre:alice",claim-role="oncall",claim-display="Alice" '
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK7nKaxTKmzX0z3V6tGqmmOvkSiG"
            "Xh3yF2J5vqkQTOY+ alice@laptop"
        )
        path = tmp_path / "authorized_keys"
        path.write_text(line + "\n", encoding="utf-8")
        resolver = AuthorizedKeysFileResolver(path)

        fp = _compute_expected_fingerprint(_SAMPLE_ED25519_OPENSSH)
        result = await resolver.resolve(fp, pubkey_blob=b"", username="alice")
        assert result is not None
        assert result.subject == "sre:alice"
        assert result.claims == {"role": "oncall", "display": "Alice"}

    async def test_unknown_options_preserved_under_options_key(self, tmp_path: Path) -> None:
        line = (
            'subject="sre:alice",no-pty,command="/bin/false" '
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK7nKaxTKmzX0z3V6tGqmmOvkSiG"
            "Xh3yF2J5vqkQTOY+ alice@laptop"
        )
        path = tmp_path / "authorized_keys"
        path.write_text(line + "\n", encoding="utf-8")
        resolver = AuthorizedKeysFileResolver(path)

        fp = _compute_expected_fingerprint(_SAMPLE_ED25519_OPENSSH)
        result = await resolver.resolve(fp, pubkey_blob=b"", username="x")
        assert result is not None
        assert result.claims["_options"] == {"no-pty": True, "command": "/bin/false"}

    async def test_miss_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "authorized_keys"
        path.write_text(_SAMPLE_ED25519_OPENSSH + "\n", encoding="utf-8")
        resolver = AuthorizedKeysFileResolver(path)

        result = await resolver.resolve(
            "SHA256:totallydifferent",
            pubkey_blob=b"",
            username="x",
        )
        assert result is None

    async def test_blank_lines_and_comments_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "authorized_keys"
        path.write_text(
            "# admins\n\n   \n# trailing comment\n" + _SAMPLE_ED25519_OPENSSH + "\n",
            encoding="utf-8",
        )
        resolver = AuthorizedKeysFileResolver(path)

        fp = _compute_expected_fingerprint(_SAMPLE_ED25519_OPENSSH)
        result = await resolver.resolve(fp, pubkey_blob=b"", username="x")
        assert result is not None
        assert result.subject == "alice@laptop"

    async def test_malformed_line_skipped_not_fatal(self, tmp_path: Path) -> None:
        """A broken line shouldn't lock everybody out."""
        path = tmp_path / "authorized_keys"
        path.write_text(
            "this is not a valid pubkey line\n" + _SAMPLE_ED25519_OPENSSH + "\n",
            encoding="utf-8",
        )
        resolver = AuthorizedKeysFileResolver(path)

        fp = _compute_expected_fingerprint(_SAMPLE_ED25519_OPENSSH)
        result = await resolver.resolve(fp, pubkey_blob=b"", username="x")
        assert result is not None
        assert result.subject == "alice@laptop"

    async def test_rotation_picked_up_on_next_call(self, tmp_path: Path) -> None:
        """File is re-read on each resolve → rotation is immediate."""
        path = tmp_path / "authorized_keys"
        path.write_text("", encoding="utf-8")
        resolver = AuthorizedKeysFileResolver(path)

        fp = _compute_expected_fingerprint(_SAMPLE_ED25519_OPENSSH)
        assert await resolver.resolve(fp, pubkey_blob=b"", username="x") is None

        # Add the key after the first call.
        path.write_text(_SAMPLE_ED25519_OPENSSH + "\n", encoding="utf-8")
        result = await resolver.resolve(fp, pubkey_blob=b"", username="x")
        assert result is not None
        assert result.subject == "alice@laptop"
