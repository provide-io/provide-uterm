#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for identity models and protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from starlette.requests import Request
from starlette.websockets import WebSocket

# We'll import these after they are moved/created
# from provide.terminal.bridge.identity import Principal, IdentityProvider

def test_principal_instantiation():
    """Verify Principal can be instantiated (TDD placeholder)."""
    # This will fail until we create the file and import it
    from provide.terminal.bridge.identity import Principal
    p = Principal(subject_id="alice", roles=frozenset(["admin"]))
    assert p.subject_id == "alice"
    assert "admin" in p.roles
    assert p.name == "alice"

def test_identity_provider_protocol():
    """Verify IdentityProvider protocol structure."""
    from provide.terminal.bridge.identity import IdentityProvider, Principal
    
    class FakeProvider:
        async def resolve_principal(self, connection: Request | WebSocket) -> Principal:
            return Principal(subject_id="bob")

    provider = FakeProvider()
    assert isinstance(provider, IdentityProvider)
