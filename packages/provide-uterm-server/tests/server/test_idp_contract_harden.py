#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Hardening of the external webhook-IdP contract (findings 1f + 1d).

1f — verify the IdP RESPONSE signature (default ON): the provider must
authenticate the webhook's JSON response, not just sign its own outbound
request. An unsigned / forged response must fall into the on_failure path.

1d — minimize forwarded headers/cookies: the provider must forward only a
curated allow-list of credentials to the external IdP, never the full set of
request headers/cookies.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from pydantic import ValidationError

from provide.uterm.server.auth import WebhookIdentityProvider
from provide.uterm.server.models import AuthConfig, ServerConfig
from provide.uterm.server.webhook_signing import build_webhook_signature, verify_webhook_signature

_SECRET = "uterm-test-secret-32-byte-minimum-key"  # pragma: allowlist secret
_URL = "https://auth.example.com/resolve"


def _signed_response(payload: dict, *, secret: str = _SECRET) -> httpx.Response:
    """Build an httpx.Response signed exactly the way the IdP must sign it."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    import time

    ts = str(time.time())
    sig = build_webhook_signature(secret, body, ts)
    return httpx.Response(
        200,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Uterm-Timestamp": ts,
            "X-Uterm-Signature": sig,
        },
    )


class _Conn:
    def __init__(self, headers: dict | None = None, cookies: dict | None = None) -> None:
        self.headers = headers or {}
        self.cookies = cookies or {}


# ---------------------------------------------------------------------------
# 1f — response signature verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_valid_response_signature_builds_principal() -> None:
    idp = WebhookIdentityProvider(url=_URL, secret=_SECRET, require_signed_response=True)
    respx.post(_URL).mock(return_value=_signed_response({"subject_id": "user-1", "roles": ["operator"]}))
    principal = await idp.resolve_principal(_Conn())
    assert principal is not None
    assert principal.subject_id == "user-1"
    assert principal.roles == frozenset({"operator"})


@pytest.mark.asyncio
@respx.mock
async def test_missing_response_signature_denied() -> None:
    idp = WebhookIdentityProvider(url=_URL, secret=_SECRET, require_signed_response=True, on_failure="deny")
    # Unsigned 200 response (no X-Uterm-Signature / X-Uterm-Timestamp headers).
    respx.post(_URL).mock(return_value=httpx.Response(200, json={"subject_id": "x", "roles": ["admin"]}))
    principal = await idp.resolve_principal(_Conn())
    assert principal is None


@pytest.mark.asyncio
@respx.mock
async def test_invalid_response_signature_denied() -> None:
    idp = WebhookIdentityProvider(url=_URL, secret=_SECRET, require_signed_response=True, on_failure="deny")
    body = json.dumps({"subject_id": "x", "roles": ["admin"]}, separators=(",", ":")).encode()
    import time

    ts = str(time.time())
    respx.post(_URL).mock(
        return_value=httpx.Response(
            200,
            content=body,
            headers={
                "X-Uterm-Timestamp": ts,
                "X-Uterm-Signature": "sha256=deadbeef",  # wrong signature
            },
        )
    )
    principal = await idp.resolve_principal(_Conn())
    assert principal is None


@pytest.mark.asyncio
@respx.mock
async def test_expired_response_signature_denied() -> None:
    idp = WebhookIdentityProvider(url=_URL, secret=_SECRET, require_signed_response=True, on_failure="deny")
    body = json.dumps({"subject_id": "x", "roles": ["admin"]}, separators=(",", ":")).encode()
    # Timestamp far in the past → fails freshness window in verify_webhook_signature.
    stale_ts = "1.0"
    sig = build_webhook_signature(_SECRET, body, stale_ts)
    respx.post(_URL).mock(
        return_value=httpx.Response(
            200,
            content=body,
            headers={"X-Uterm-Timestamp": stale_ts, "X-Uterm-Signature": sig},
        )
    )
    principal = await idp.resolve_principal(_Conn())
    assert principal is None


# ---------------------------------------------------------------------------
# L8a — verifying with an EMPTY key must be impossible (fail closed)
# ---------------------------------------------------------------------------


def test_verify_webhook_signature_empty_string_secret_is_false() -> None:
    """A signature crafted with an empty HMAC key must not validate.

    Without this guard, ``verify_webhook_signature("", ...)`` HMACs with an
    empty key — an attacker who knows the body + timestamp can forge a valid
    signature. With no key there is nothing to authenticate against, so the
    function must fail closed regardless of caller.
    """
    import time

    body = b'{"subject_id":"x"}'
    ts = str(time.time())
    # A signature an attacker can compute knowing only the body + timestamp.
    forged = build_webhook_signature("", body, ts)
    assert verify_webhook_signature("", body, forged, ts) is False


def test_verify_webhook_signature_none_secret_is_false() -> None:
    """``secret=None`` (no key configured) also fails closed."""
    import time

    body = b'{"subject_id":"x"}'
    ts = str(time.time())
    forged = build_webhook_signature("", body, ts)
    assert verify_webhook_signature(None, body, forged, ts) is False  # type: ignore[arg-type]


def test_verify_webhook_signature_whitespace_secret_is_false() -> None:
    """A whitespace-only secret has no usable key material → fail closed."""
    import time

    body = b'{"subject_id":"x"}'
    ts = str(time.time())
    forged = build_webhook_signature("   ", body, ts)
    assert verify_webhook_signature("   ", body, forged, ts) is False


@pytest.mark.asyncio
@respx.mock
async def test_provider_with_no_secret_rejects_forged_signed_response() -> None:
    """A provider built directly with secret=None + require_signed_response=True
    must REJECT a response carrying an attacker-forged empty-key signature.

    This is the non-config construction path (test/embedder) where the config
    validator never ran; the security-critical function must defend itself.
    """
    idp = WebhookIdentityProvider(url=_URL, secret=None, require_signed_response=True, on_failure="deny")
    import time

    body = json.dumps({"subject_id": "attacker", "roles": ["admin"]}, separators=(",", ":")).encode()
    ts = str(time.time())
    # Attacker forges a signature with an empty key (what self.secret or "" would use).
    forged = build_webhook_signature("", body, ts)
    respx.post(_URL).mock(
        return_value=httpx.Response(
            200,
            content=body,
            headers={"X-Uterm-Timestamp": ts, "X-Uterm-Signature": forged},
        )
    )
    principal = await idp.resolve_principal(_Conn())
    # Forged response lands in the on_failure (deny) path → None, NOT an admin.
    assert principal is None


@pytest.mark.asyncio
@respx.mock
async def test_invalid_response_signature_viewer_on_failure() -> None:
    idp = WebhookIdentityProvider(url=_URL, secret=_SECRET, require_signed_response=True, on_failure="viewer")
    respx.post(_URL).mock(return_value=httpx.Response(200, json={"subject_id": "x", "roles": ["admin"]}))
    principal = await idp.resolve_principal(_Conn())
    assert principal is not None
    assert principal.subject_id == "anonymous"
    assert principal.roles == frozenset({"viewer"})


@pytest.mark.asyncio
@respx.mock
async def test_invalid_response_signature_emits_audit_event(monkeypatch) -> None:
    import provide.uterm.server.auth as auth_mod

    idp = WebhookIdentityProvider(url=_URL, secret=_SECRET, require_signed_response=True, on_failure="deny")
    respx.post(_URL).mock(return_value=httpx.Response(200, json={"subject_id": "x", "roles": ["admin"]}))

    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(auth_mod, "audit_event", lambda action, **kw: captured.append((action, kw)))

    principal = await idp.resolve_principal(_Conn())
    assert principal is None
    assert "auth.webhook_idp_failure" in [a for a, _ in captured]


@pytest.mark.asyncio
@respx.mock
async def test_require_signed_response_false_accepts_unsigned() -> None:
    """Legacy behaviour: when verification disabled, an unsigned response is trusted."""
    idp = WebhookIdentityProvider(url=_URL, secret=_SECRET, require_signed_response=False)
    respx.post(_URL).mock(return_value=httpx.Response(200, json={"subject_id": "legacy", "roles": ["viewer"]}))
    principal = await idp.resolve_principal(_Conn())
    assert principal is not None
    assert principal.subject_id == "legacy"


@pytest.mark.asyncio
@respx.mock
async def test_valid_signature_uses_verified_response_bytes() -> None:
    """The principal is built from the same RAW bytes that were signature-verified."""
    payload = {"subject_id": "raw-bytes-user", "roles": ["admin"], "claims": {"k": "v"}}
    idp = WebhookIdentityProvider(url=_URL, secret=_SECRET, require_signed_response=True)
    respx.post(_URL).mock(return_value=_signed_response(payload))
    principal = await idp.resolve_principal(_Conn())
    assert principal is not None
    assert principal.subject_id == "raw-bytes-user"
    assert principal.claims["k"] == "v"


# ---------------------------------------------------------------------------
# 1f — config validator
# ---------------------------------------------------------------------------


def test_webhook_require_signed_response_default_true() -> None:
    config = AuthConfig(webhook_idp_require_signed_response=True, webhook_idp_secret=_SECRET)
    assert config.webhook_idp_require_signed_response is True
    # Default value is True (secure-by-default).
    assert AuthConfig().webhook_idp_require_signed_response is True


def test_webhook_require_signed_response_without_secret_rejected() -> None:
    with pytest.raises(ValidationError, match="webhook_idp_secret"):
        AuthConfig(identity_provider="webhook", webhook_idp_require_signed_response=True)


def test_webhook_require_signed_response_with_secret_ok() -> None:
    config = AuthConfig(
        identity_provider="webhook",
        webhook_idp_require_signed_response=True,
        webhook_idp_secret=_SECRET,
    )
    assert config.webhook_idp_require_signed_response is True


def test_webhook_require_signed_response_false_no_secret_ok() -> None:
    # Opt out of verification → no secret required.
    config = AuthConfig(identity_provider="webhook", webhook_idp_require_signed_response=False)
    assert config.webhook_idp_require_signed_response is False


def test_local_idp_require_signed_response_no_secret_ok() -> None:
    # The require-secret rule only applies to the webhook identity provider.
    config = AuthConfig(identity_provider="local", webhook_idp_require_signed_response=True)
    assert config.identity_provider == "local"


# ---------------------------------------------------------------------------
# 1d — curated forward allow-list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_only_allowlisted_headers_and_cookies_forwarded() -> None:
    idp = WebhookIdentityProvider(
        url=_URL,
        secret=_SECRET,
        require_signed_response=False,
        forward_headers=frozenset({"authorization", "x-uterm-principal"}),
        forward_cookies=frozenset({"uterm_token"}),
    )
    route = respx.post(_URL).mock(return_value=httpx.Response(200, json={"subject_id": "u", "roles": ["viewer"]}))
    conn = _Conn(
        headers={
            "Authorization": "Bearer abc",
            "X-Uterm-Principal": "alice",
            "X-Internal-Route": "leak-me",
        },
        cookies={"uterm_token": "tok", "session": "should-be-dropped"},
    )
    await idp.resolve_principal(conn)
    sent = json.loads(route.calls.last.request.content)
    # Case-insensitive: incoming mixed-case header keys match the lowercase allow-list.
    fwd_headers = {k.lower() for k in sent["headers"]}
    assert "authorization" in fwd_headers
    assert "x-uterm-principal" in fwd_headers
    assert "x-internal-route" not in fwd_headers
    assert set(sent["cookies"]) == {"uterm_token"}
    assert "session" not in sent["cookies"]
    assert sent["action"] == "resolve_principal"


@pytest.mark.asyncio
@respx.mock
async def test_operator_extended_forward_header_passes() -> None:
    idp = WebhookIdentityProvider(
        url=_URL,
        secret=_SECRET,
        require_signed_response=False,
        forward_headers=frozenset({"authorization", "x-tenant"}),
        forward_cookies=frozenset(),
    )
    route = respx.post(_URL).mock(return_value=httpx.Response(200, json={"subject_id": "u", "roles": ["viewer"]}))
    conn = _Conn(headers={"X-Tenant": "acme", "X-Other": "drop"})
    await idp.resolve_principal(conn)
    sent = json.loads(route.calls.last.request.content)
    fwd_headers = {k.lower() for k in sent["headers"]}
    assert "x-tenant" in fwd_headers
    assert "x-other" not in fwd_headers


@pytest.mark.asyncio
@respx.mock
async def test_empty_allowlists_forward_nothing() -> None:
    idp = WebhookIdentityProvider(
        url=_URL,
        secret=_SECRET,
        require_signed_response=False,
        forward_headers=frozenset(),
        forward_cookies=frozenset(),
    )
    route = respx.post(_URL).mock(return_value=httpx.Response(200, json={"subject_id": "u", "roles": ["viewer"]}))
    conn = _Conn(headers={"Authorization": "Bearer abc"}, cookies={"uterm_token": "tok"})
    await idp.resolve_principal(conn)
    sent = json.loads(route.calls.last.request.content)
    assert sent["headers"] == {}
    assert sent["cookies"] == {}


@pytest.mark.asyncio
@respx.mock
async def test_default_allowlists_empty_when_unset() -> None:
    """When the provider is constructed without explicit allow-lists, it forwards
    nothing (secure default) rather than every header/cookie."""
    idp = WebhookIdentityProvider(url=_URL, secret=_SECRET, require_signed_response=False)
    route = respx.post(_URL).mock(return_value=httpx.Response(200, json={"subject_id": "u", "roles": ["viewer"]}))
    conn = _Conn(headers={"Authorization": "Bearer abc"}, cookies={"uterm_token": "tok"})
    await idp.resolve_principal(conn)
    sent = json.loads(route.calls.last.request.content)
    assert sent["headers"] == {}
    assert sent["cookies"] == {}


# ---------------------------------------------------------------------------
# factory wiring (1d/1f end-to-end)
# ---------------------------------------------------------------------------


def test_factory_wires_allowlists_and_signing(monkeypatch) -> None:
    from provide.uterm.server.app import create_server_app

    config = ServerConfig(
        auth=AuthConfig(
            identity_provider="webhook",
            mode="dev_token",
            webhook_idp_url="http://localhost:8080/auth",
            webhook_idp_secret=_SECRET,
            webhook_idp_require_signed_response=True,
            webhook_idp_forward_headers=["x-tenant"],
            webhook_idp_forward_cookies=["extra_cookie"],
        )
    )
    app = create_server_app(config, api_only=True)
    idp = app.state.uterm_hub.identity_provider
    assert isinstance(idp, WebhookIdentityProvider)
    assert idp.require_signed_response is True
    # Curated header allow-list: the standard auth credentials + the operator extension.
    assert "authorization" in idp.forward_headers
    assert "x-api-key" in idp.forward_headers
    assert "x-uterm-principal" in idp.forward_headers
    assert "x-uterm-role" in idp.forward_headers
    assert "x-tenant" in idp.forward_headers
    # Cookie allow-list: token/principal/role cookies + operator extension.
    assert "uterm_token" in idp.forward_cookies
    assert "uterm_principal" in idp.forward_cookies
    assert "uterm_role" in idp.forward_cookies
    assert "extra_cookie" in idp.forward_cookies
