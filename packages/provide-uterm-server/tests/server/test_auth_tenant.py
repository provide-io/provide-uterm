#
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Canonical tenant identity resolution across authentication modes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from provide.uterm.server.api_keys import ApiKeyStore
from provide.uterm.server.auth import LocalIdentityProvider, WebhookIdentityProvider
from provide.uterm.server.config_schema import AuthConfig
from provide.uterm.server.dev_idp import setup_dev_idp


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


def test_header_tenant_precedes_cookie_and_custom_cookie_is_supported() -> None:
    auth = AuthConfig(mode="header", tenant_header="x-organization", tenant_cookie="org_tenant")
    provider = LocalIdentityProvider(auth)

    header = provider._principal_from_header_auth(
        {"x-uterm-principal": "alice", "x-organization": "tenant-header"},
        {"org_tenant": "tenant-cookie"},
    )
    cookie = provider._principal_from_header_auth(
        {"x-uterm-principal": "alice"},
        {"org_tenant": "tenant-cookie"},
    )

    assert header.tenant_id == "tenant-header"
    assert cookie.tenant_id == "tenant-cookie"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("jwt_tenant_claim", ""),
        ("jwt_tenant_claim", " tenant"),
        ("jwt_tenant_claim", "tenant\nclaim"),
        ("tenant_header", "bad header"),
        ("tenant_header", "x-tenant\r\nspoof"),
        ("tenant_cookie", "bad cookie"),
        ("tenant_cookie", "tenant;admin"),
    ],
)
def test_tenant_source_names_are_strict(field: str, value: str) -> None:
    with pytest.raises(ValueError, match=field):
        AuthConfig(**{field: value})


@pytest.mark.parametrize("unsafe", [" bad ", "tenant\nspoof", "租戶", "a" * 129])
def test_malformed_header_or_cookie_tenant_denies_without_fallback(unsafe: str) -> None:
    auth = AuthConfig(mode="header", tenant_header="x-tenant", tenant_cookie="tenant")
    provider = LocalIdentityProvider(auth)

    with pytest.raises(ValueError, match="tenant"):
        provider._principal_from_header_auth(
            {"x-uterm-principal": "alice", "x-tenant": unsafe},
            {"tenant": "tenant-safe"},
        )
    with pytest.raises(ValueError, match="tenant"):
        provider._principal_from_header_auth({"x-uterm-principal": "alice"}, {"tenant": unsafe})


def test_header_resolver_turns_invalid_tenant_into_explicit_anonymous_denial() -> None:
    auth = AuthConfig(mode="header", tenant_header="x-tenant", tenant_cookie="tenant")
    connection = type(
        "Connection",
        (),
        {
            "headers": {"x-uterm-principal": "alice", "x-tenant": "bad tenant"},
            "cookies": {"tenant": "tenant-safe"},
        },
    )()

    principal = LocalIdentityProvider(auth).resolve_principal_sync(connection)

    assert principal == principal.anonymous()


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


def test_api_key_principal_propagates_issuer_tenant() -> None:
    store = ApiKeyStore()
    raw_key, _record = store.create("automation", scopes=frozenset({"operator"}), tenant_id="tenant-a")
    provider = LocalIdentityProvider(AuthConfig(api_keys_enabled=True), store)

    principal = provider._principal_from_api_key({"x-api-key": raw_key})

    assert principal is not None
    assert principal.tenant_id == "tenant-a"


def test_dev_token_emits_configured_tenant_claim(tmp_path: Path) -> None:
    import jwt

    auth = AuthConfig(mode="dev_token", dev_tenant_id="tenant-dev", jwt_tenant_claim="organization_id")
    token = setup_dev_idp(auth, token_path=tmp_path / "token")

    claims = jwt.decode(
        token,
        auth.jwt_public_key_pem,
        algorithms=["HS256"],
        audience=auth.jwt_audience,
        issuer=auth.jwt_issuer,
    )
    assert claims["organization_id"] == "tenant-dev"
