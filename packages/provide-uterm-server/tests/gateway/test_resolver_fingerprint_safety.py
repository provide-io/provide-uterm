#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Gap #3 safety tests: resolver fingerprint disagreement with actual client pubkey.

Documents desired behaviour when a misbehaving ``SSHKeyResolver`` returns a
``ResolvedIdentity`` whose ``fingerprint`` field disagrees with the client's
actual pubkey fingerprint.

Current code in ``_make_no_auth_server_class`` only fills in fingerprint when
the resolver left it blank::

    if not identity.fingerprint:
        identity = ResolvedIdentity(subject=..., claims=..., fingerprint=fp)

If the resolver returns a mismatching fingerprint the gateway forwards it
downstream unchanged — an audit-log lie.

Test (1) is EXPECTED to fail against current code. That failure is the signal.
Tests (2) and (3) document already-passing behaviour as regression protection.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from provide.uterm.auth import ResolvedIdentity
from provide.uterm.gateway._ssh_handler import _make_no_auth_server_class

pytestmark = pytest.mark.asyncio

_ACTUAL_FP = "SHA256:actual-client-key-fingerprint"
_FORGED_FP = "SHA256:FAKE-DOES-NOT-MATCH"


class _FingerprintResolver:
    """Resolver that returns a preset identity regardless of input fingerprint."""

    def __init__(self, identity: ResolvedIdentity) -> None:
        self.identity = identity

    async def resolve(
        self,
        fingerprint: str,
        *,
        pubkey_blob: bytes,
        username: str,
    ) -> ResolvedIdentity | None:
        return self.identity


def _fake_ssh_key(fingerprint: str = _ACTUAL_FP) -> MagicMock:
    """Mock an asyncssh key object with the methods the handler introspects."""
    key = MagicMock()
    key.get_fingerprint = MagicMock(return_value=fingerprint)
    key.export_public_key = MagicMock(return_value=b"ssh-ed25519 AAAA...")
    return key


class TestFingerprintSafety:
    async def test_forged_fingerprint_is_overwritten_with_actual_key_fp(self) -> None:
        """SAFETY GAP: resolver returns a forged fingerprint that doesn't match the key.

        Desired behaviour: the gateway MUST ignore the resolver's fingerprint and
        replace it with the real client-key fingerprint so the audit log is accurate.

        This test is EXPECTED TO FAIL against current code because the existing guard
        only fires when ``identity.fingerprint`` is empty — a non-empty forged value
        passes through unchanged.
        """
        identity_with_forged_fp = ResolvedIdentity(
            subject="sre:alice",
            claims={"role": "oncall"},
            fingerprint=_FORGED_FP,
        )
        resolver = _FingerprintResolver(identity_with_forged_fp)
        cls = _make_no_auth_server_class(resolver)
        server = cls()
        key = _fake_ssh_key(_ACTUAL_FP)

        accepted = await server.validate_public_key("alice", key)

        assert accepted is True
        assert server._resolved_identity is not None
        # The gateway must always use the actual client key's fingerprint,
        # never the resolver's claim — a resolver cannot be trusted to report
        # the correct fingerprint for a key it didn't generate.
        assert server._resolved_identity.fingerprint == _ACTUAL_FP, (
            f"Expected actual key fingerprint {_ACTUAL_FP!r} but got "
            f"{server._resolved_identity.fingerprint!r} — "
            "the gateway is forwarding the resolver's forged fingerprint downstream."
        )

    async def test_empty_fingerprint_is_filled_from_actual_key(self) -> None:
        """Existing behaviour: resolver leaves fingerprint empty → gateway fills it in.

        This is the current well-behaved path. Documented here as regression protection.
        """
        identity_no_fp = ResolvedIdentity(
            subject="sre:bob",
            claims={},
            fingerprint="",
        )
        resolver = _FingerprintResolver(identity_no_fp)
        cls = _make_no_auth_server_class(resolver)
        server = cls()
        key = _fake_ssh_key(_ACTUAL_FP)

        accepted = await server.validate_public_key("bob", key)

        assert accepted is True
        assert server._resolved_identity is not None
        assert server._resolved_identity.fingerprint == _ACTUAL_FP

    async def test_matching_fingerprint_from_resolver_is_preserved(self) -> None:
        """Resolver returns the correct fingerprint (it did its homework).

        Whether the gateway overwrites-always or only-when-empty, the stashed
        fingerprint must equal the actual client key's fingerprint in either case.
        Documents this invariant as regression protection.
        """
        identity_correct_fp = ResolvedIdentity(
            subject="sre:carol",
            claims={"tier": "gold"},
            fingerprint=_ACTUAL_FP,  # matches the real key
        )
        resolver = _FingerprintResolver(identity_correct_fp)
        cls = _make_no_auth_server_class(resolver)
        server = cls()
        key = _fake_ssh_key(_ACTUAL_FP)

        accepted = await server.validate_public_key("carol", key)

        assert accepted is True
        assert server._resolved_identity is not None
        # Whether the gateway preserves the resolver's value or overwrites it,
        # the result must equal the actual key fingerprint (they agree here).
        assert server._resolved_identity.fingerprint == _ACTUAL_FP
