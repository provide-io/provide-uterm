#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for the SSHKeyResolver→gateway wiring.

Verifies that :func:`_make_no_auth_server_class` consults a configured
resolver in its async ``validate_public_key`` and stashes the resulting
:class:`ResolvedIdentity` on the server instance so the process handler
can forward it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from provide.uterm.auth import ResolvedIdentity, SSHKeyResolver
from provide.uterm.gateway._ssh_handler import _make_no_auth_server_class

pytestmark = pytest.mark.asyncio


class _FixedResolver:
    """Resolver that returns a preset identity for matching fingerprints."""

    def __init__(self, identity: ResolvedIdentity | None) -> None:
        self.identity = identity
        self.calls: list[tuple[str, bytes, str]] = []

    async def resolve(
        self,
        fingerprint: str,
        *,
        pubkey_blob: bytes,
        username: str,
    ) -> ResolvedIdentity | None:
        self.calls.append((fingerprint, pubkey_blob, username))
        return self.identity


def _fake_ssh_key(fingerprint: str = "SHA256:fake") -> MagicMock:
    """Mock an asyncssh key object with the methods the handler introspects."""
    key = MagicMock()
    key.get_fingerprint = MagicMock(return_value=fingerprint)
    key.export_public_key = MagicMock(return_value=b"ssh-ed25519 AAAA...")
    return key


class TestResolverIntegration:
    async def test_no_resolver_preserves_historical_accept_all(self) -> None:
        """Default construction → any pubkey accepted, no identity stashed."""
        cls = _make_no_auth_server_class()
        server = cls()
        accepted = await server.validate_public_key("guest", _fake_ssh_key())
        assert accepted is True
        assert server._resolved_identity is None

    async def test_resolver_hit_stashes_identity(self) -> None:
        identity = ResolvedIdentity(subject="sre:alice", claims={"role": "oncall"})
        resolver = _FixedResolver(identity)
        cls = _make_no_auth_server_class(resolver)
        server = cls()

        accepted = await server.validate_public_key("alice", _fake_ssh_key("SHA256:abc"))
        assert accepted is True
        assert server._resolved_identity is not None
        assert server._resolved_identity.subject == "sre:alice"
        # Fingerprint was auto-populated from the key.
        assert server._resolved_identity.fingerprint == "SHA256:abc"
        # Resolver saw the right args.
        assert resolver.calls[0][0] == "SHA256:abc"
        assert resolver.calls[0][2] == "alice"

    async def test_resolver_miss_falls_through_when_not_required(self) -> None:
        """Default require_resolver=False → miss returns True (allow password auth)."""
        resolver = _FixedResolver(None)
        cls = _make_no_auth_server_class(resolver, require_resolver=False)
        server = cls()
        accepted = await server.validate_public_key("bob", _fake_ssh_key())
        assert accepted is True  # fall through to password auth
        assert server._resolved_identity is None

    async def test_resolver_miss_rejects_when_required(self) -> None:
        """require_resolver=True → miss returns False (rejects the key outright)."""
        resolver = _FixedResolver(None)
        cls = _make_no_auth_server_class(resolver, require_resolver=True)
        server = cls()
        accepted = await server.validate_public_key("mallory", _fake_ssh_key())
        assert accepted is False
        assert server._resolved_identity is None

    async def test_require_resolver_disables_password_and_kbdint(self) -> None:
        """Mandatory-resolver config must shut off other auth methods,
        so an unknown key can't sneak through by falling back to password."""
        resolver = _FixedResolver(None)
        cls = _make_no_auth_server_class(resolver, require_resolver=True)
        server = cls()
        assert server.password_auth_supported() is False
        assert server.kbdint_auth_supported() is False
        assert server.public_key_auth_supported() is True

    async def test_password_still_enabled_when_resolver_optional(self) -> None:
        resolver = _FixedResolver(None)
        cls = _make_no_auth_server_class(resolver, require_resolver=False)
        server = cls()
        assert server.password_auth_supported() is True
        assert server.kbdint_auth_supported() is True

    async def test_resolver_fingerprint_is_overwritten_by_actual_key(self) -> None:
        """Whatever fingerprint the resolver reports, the gateway overwrites
        it with the *actual* client-key fingerprint — otherwise a buggy or
        malicious resolver could forge audit-log entries. The resolver is
        authoritative for subject + claims; the gateway is authoritative
        for the fingerprint (see test_resolver_fingerprint_safety.py for
        the dedicated safety suite)."""
        identity = ResolvedIdentity(
            subject="x",
            claims={},
            fingerprint="SHA256:whatever-resolver-said",
        )
        resolver = _FixedResolver(identity)
        cls = _make_no_auth_server_class(resolver)
        server = cls()
        await server.validate_public_key("u", _fake_ssh_key("SHA256:actual-key-fp"))
        assert server._resolved_identity is not None
        # Actual client-key fingerprint wins — resolver-provided value is
        # treated as untrusted for the fingerprint field specifically.
        assert server._resolved_identity.fingerprint == "SHA256:actual-key-fp"

    async def test_protocol_conformance_of_custom_resolver(self) -> None:
        """Custom resolvers that quack like the Protocol must satisfy isinstance."""
        resolver = _FixedResolver(None)
        assert isinstance(resolver, SSHKeyResolver)
