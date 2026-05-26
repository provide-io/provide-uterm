#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import pytest

from provide.uterm.server.auth import LocalIdentityProvider
from provide.uterm.server.models import AuthConfig


def test_local_idp_instantiation():
    auth = AuthConfig(mode="jwt")
    idp = LocalIdentityProvider(auth)
    assert idp.auth == auth


@pytest.mark.asyncio
async def test_local_idp_resolve_anonymous():
    auth = AuthConfig(mode="jwt")
    idp = LocalIdentityProvider(auth)

    class MockConnection:
        headers = {}
        cookies = {}

    principal = await idp.resolve_principal(MockConnection())
    assert principal.subject_id == "anonymous"


@pytest.mark.asyncio
async def test_local_idp_resolve_api_key_principal_short_circuits() -> None:
    """``resolve_principal_sync`` returns the API-key principal before falling through to mode-based auth."""
    from provide.uterm.server.api_keys import ApiKeyStore

    store = ApiKeyStore()
    raw_key, _record = store.create("admin-key", scopes=frozenset({"admin"}))
    auth = AuthConfig(api_keys_enabled=True, mode="dev_token")
    idp = LocalIdentityProvider(auth, api_key_store=store)

    class MockConnection:
        headers = {"x-api-key": raw_key}
        cookies: dict[str, str] = {}

    principal = await idp.resolve_principal(MockConnection())
    assert principal.subject_id != "anonymous"
    assert "admin" in principal.roles
