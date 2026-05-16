#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import pytest

from provide.uterm.server.auth import LocalIdentityProvider
from provide.uterm.server.models import AuthConfig


def test_local_idp_instantiation():
    auth = AuthConfig(mode="none")
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
