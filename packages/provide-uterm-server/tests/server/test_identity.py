#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for identity models and protocol."""

from __future__ import annotations

from starlette.requests import Request
from starlette.websockets import WebSocket

# We'll import these after they are moved/created
# from provide.uterm.server.bridge.identity import Principal, IdentityProvider


def test_principal_instantiation():
    """Verify Principal can be instantiated (TDD placeholder)."""
    # This will fail until we create the file and import it
    from provide.uterm.server.bridge.identity import Principal

    p = Principal(subject_id="alice", roles=frozenset(["admin"]))
    assert p.subject_id == "alice"
    assert "admin" in p.roles
    assert p.name == "alice"


def test_principal_carries_canonical_tenant_identity() -> None:
    from provide.uterm.server.bridge.identity import Principal

    principal = Principal(subject_id="alice", tenant_id="tenant-a")

    assert principal.tenant_id == "tenant-a"


def test_named_tenantless_principals_make_policy_explicit() -> None:
    from provide.uterm.server.bridge.identity import Principal

    assert Principal.anonymous().tenant_id is None
    assert Principal.system_worker().tenant_id is None


def test_identity_provider_protocol():
    """Verify IdentityProvider protocol structure."""
    from provide.uterm.server.bridge.identity import IdentityProvider, Principal

    class FakeProvider:
        async def resolve_principal(self, connection: Request | WebSocket) -> Principal:
            return Principal(subject_id="bob")

    provider = FakeProvider()
    assert isinstance(provider, IdentityProvider)
