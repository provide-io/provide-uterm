#
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Canonical tenant identity resolution across authentication modes."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
import respx

from provide.uterm.server.auth import LocalIdentityProvider, WebhookIdentityProvider
from provide.uterm.server.config_schema import AuthConfig


def test_jwt_principal_uses_configured_tenant_claim() -> None:
    auth = AuthConfig(jwt_public_key_pem="secret", jwt_tenant_claim="organization_id")
    provider = LocalIdentityProvider(auth)
    with (
        patch.object(provider, "_resolve_jwt_key", return_value="secret"),
        patch(
            "jwt.decode",
            return_value={"sub": "alice", "exp": 9999999999, "organization_id": "tenant-a"},
        ),
    ):
        principal = provider._principal_from_jwt_token("token")

    assert principal.tenant_id == "tenant-a"


def test_header_principal_uses_configured_tenant_header() -> None:
    auth = AuthConfig(mode="header", tenant_header="x-organization")

    principal = LocalIdentityProvider(auth)._principal_from_header_auth(
        {"x-uterm-principal": "alice", "x-uterm-role": "operator", "x-organization": "tenant-a"}, {}
    )

    assert principal.tenant_id == "tenant-a"


@pytest.mark.asyncio
@respx.mock
async def test_webhook_principal_uses_tenant_id() -> None:
    url = "https://auth.example.com/resolve"
    respx.post(url).mock(return_value=httpx.Response(200, json={"subject_id": "alice", "tenant_id": "tenant-a"}))

    principal = await WebhookIdentityProvider(url, require_signed_response=False).resolve_principal(
        type("Connection", (), {"headers": {}, "cookies": {}})()
    )

    assert principal is not None
    assert principal.tenant_id == "tenant-a"


@pytest.mark.parametrize("tenant_id", ["", " bad ", 123, ["tenant-a"]])
def test_jwt_rejects_malformed_tenant_claim(tenant_id: object) -> None:
    auth = AuthConfig(jwt_public_key_pem="secret")
    provider = LocalIdentityProvider(auth)
    with (
        patch.object(provider, "_resolve_jwt_key", return_value="secret"),
        patch("jwt.decode", return_value={"sub": "alice", "exp": 9999999999, "tenant_id": tenant_id}),
        pytest.raises(ValueError, match="tenant"),
    ):
        provider._principal_from_jwt_token("token")
