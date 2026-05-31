#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for server auth.py — principal resolution and JWT helpers."""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest

_TEST_KEY = "uterm-test-secret-32-byte-minimum-key"


def _make_token(
    sub: str = "user1",
    roles: Any = None,
    exp_offset: int = 600,
    key: str = _TEST_KEY,
) -> str:
    if roles is None:
        roles = ["operator"]
    now = int(time.time())
    payload = {
        "sub": sub,
        "roles": roles,
        "iss": "provide-uterm",
        "aud": "provide-uterm-server",
        "iat": now,
        "nbf": now,
        "exp": now + exp_offset,
    }
    return jwt.encode(payload, key=key, algorithm="HS256")


def _jwt_auth_config(key: str = _TEST_KEY):  # type: ignore[return]
    from provide.uterm.server.models import AuthConfig

    return AuthConfig(
        mode="jwt",
        jwt_public_key_pem=key,
        jwt_algorithms=["HS256"],
        jwt_issuer="provide-uterm",
        jwt_audience="provide-uterm-server",
        worker_bearer_token=_make_token(sub="worker", roles=["admin"]),
    )


class TestHeaderModeTrustedProxyEnforcement:
    """At request time, header-mode auth respects the trusted_proxy_ips allowlist."""

    def _config(self, trusted: list[str] | None = None):  # type: ignore[return]
        from provide.uterm.server.models import AuthConfig

        return AuthConfig(
            mode="header",
            worker_bearer_token=_make_token(),
            trusted_proxy_ips=list(trusted or []),
        )

    def test_header_role_respected_from_trusted_source(self) -> None:
        from types import SimpleNamespace

        from provide.uterm.server.auth import LocalIdentityProvider

        idp = LocalIdentityProvider(self._config(["10.0.0.5"]))
        conn = SimpleNamespace(
            headers={"x-uterm-principal": "bob", "x-uterm-role": "operator"},
            cookies={},
            client=SimpleNamespace(host="10.0.0.5"),
        )
        principal = idp.resolve_principal_sync(conn)  # type: ignore[arg-type]
        assert principal.subject_id == "bob"
        assert "operator" in principal.roles

    def test_header_role_rejected_from_untrusted_source(self) -> None:
        from types import SimpleNamespace

        from provide.uterm.server.auth import LocalIdentityProvider

        idp = LocalIdentityProvider(self._config(["10.0.0.5"]))
        conn = SimpleNamespace(
            headers={"x-uterm-principal": "bob", "x-uterm-role": "admin"},
            cookies={},
            client=SimpleNamespace(host="8.8.8.8"),
        )
        principal = idp.resolve_principal_sync(conn)  # type: ignore[arg-type]
        # Untrusted source → anonymous viewer, NOT the claimed admin role.
        assert principal.subject_id == "anonymous"
        assert principal.roles == frozenset({"viewer"})

    def test_no_trusted_proxy_ips_falls_through_to_header_handler(self) -> None:
        # With trusted_proxy_ips empty (loopback bind), header auth runs as before.
        from types import SimpleNamespace

        from provide.uterm.server.auth import LocalIdentityProvider

        idp = LocalIdentityProvider(self._config([]))
        conn = SimpleNamespace(
            headers={"x-uterm-principal": "alice", "x-uterm-role": "operator"},
            cookies={},
            client=SimpleNamespace(host="127.0.0.1"),
        )
        principal = idp.resolve_principal_sync(conn)  # type: ignore[arg-type]
        assert principal.subject_id == "alice"


# ---------------------------------------------------------------------------
# Finding #7: WebhookIdentityProvider failure mode
# ---------------------------------------------------------------------------


class TestWebhookIdpOnFailure:
    """Webhook failures must default to deny, not fail-open as viewer."""

    async def test_default_on_failure_is_deny(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from provide.uterm.server.auth import WebhookIdentityProvider

        idp = WebhookIdentityProvider("https://example.com/auth")
        conn = SimpleNamespace(headers={}, cookies={})
        with patch("httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.__aenter__.return_value = instance
            instance.post = AsyncMock(side_effect=RuntimeError("network down"))
            mock_client_cls.return_value = instance
            result = await idp.resolve_principal(conn)  # type: ignore[arg-type]
        # Finding #7: webhook down → None (caller drops to anonymous/401).
        assert result is None

    async def test_viewer_on_failure_preserves_legacy(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from provide.uterm.server.auth import WebhookIdentityProvider

        idp = WebhookIdentityProvider("https://example.com/auth", on_failure="viewer")
        conn = SimpleNamespace(headers={}, cookies={})
        with patch("httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.__aenter__.return_value = instance
            instance.post = AsyncMock(side_effect=RuntimeError("network down"))
            mock_client_cls.return_value = instance
            result = await idp.resolve_principal(conn)  # type: ignore[arg-type]
        assert result is not None
        assert result.subject_id == "anonymous"
        assert "viewer" in result.roles

    def test_invalid_on_failure_rejected(self) -> None:
        from provide.uterm.server.auth import WebhookIdentityProvider

        with pytest.raises(ValueError, match="on_failure must be"):
            WebhookIdentityProvider("https://example.com/auth", on_failure="allow")


# ---------------------------------------------------------------------------
# Finding #8: WebhookAuthorizationProvider.is_admin delegates to webhook
# ---------------------------------------------------------------------------


class TestWebhookAuthzIsAdmin:
    async def test_is_admin_consults_webhook(self) -> None:
        from unittest.mock import AsyncMock, patch

        from provide.uterm.server.authorization import (
            AuthorizationService,
            WebhookAuthorizationProvider,
        )
        from provide.uterm.server.bridge.identity import Principal

        provider = WebhookAuthorizationProvider("https://example.com/authz")
        service = AuthorizationService(provider)
        # A principal whose JWT role says ``admin`` — local fallback would
        # accept this without ever asking the policy engine.  The webhook
        # must be the source of truth instead.
        principal = Principal(subject_id="bob", roles=frozenset({"admin"}))

        class _Resp:
            status_code = 200

            def json(self):
                return {"allow": False}

        with patch("httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.__aenter__.return_value = instance
            instance.post = AsyncMock(return_value=_Resp())
            mock_client_cls.return_value = instance
            import json as _json

            allowed = await service.is_admin(principal)
            # Webhook said no → not admin, even though roles claim admin.
            assert allowed is False
            assert instance.post.await_count == 1
            # Now sends pre-serialized bytes via ``content=`` (not ``json=``).
            sent_payload = _json.loads(instance.post.await_args.kwargs["content"])
            assert sent_payload["action"] == "admin"


# ---------------------------------------------------------------------------
# L25: WebhookAuthorizationProvider.resolve_browser_role filters the returned
# role to the known allow-list (consistent with the IDP role-resolution path).
# ---------------------------------------------------------------------------


class TestWebhookAuthzResolveBrowserRoleFiltered:
    @staticmethod
    async def _resolve_with_role(returned_role) -> str:
        from unittest.mock import AsyncMock, patch

        from provide.uterm.server.authorization import WebhookAuthorizationProvider
        from provide.uterm.server.bridge.identity import Principal
        from provide.uterm.server.models import SessionDefinition

        provider = WebhookAuthorizationProvider("https://example.com/authz")
        principal = Principal(subject_id="bob", roles=frozenset({"viewer"}))
        session = SessionDefinition(session_id="s1", display_name="s1", connector_type="shell")

        class _Resp:
            status_code = 200

            def json(self):
                return {"role": returned_role}

        with patch("httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            instance.__aenter__.return_value = instance
            instance.post = AsyncMock(return_value=_Resp())
            mock_client_cls.return_value = instance
            return await provider.resolve_browser_role(principal, session)

    async def test_bogus_role_falls_back_to_viewer(self) -> None:
        """A non-allow-list role string (e.g. ``superuser``) must not leak
        through — it is filtered to the default ``viewer``."""
        assert await self._resolve_with_role("superuser") == "viewer"

    async def test_mixed_case_admin_is_case_folded(self) -> None:
        """A legitimate but mixed-case role is normalized to the canonical
        lower-cased allow-list entry."""
        assert await self._resolve_with_role("Admin") == "admin"

    async def test_garbage_role_falls_back_to_viewer(self) -> None:
        """Whitespace/garbage outside the allow-list resolves to ``viewer``."""
        assert await self._resolve_with_role("  not-a-role  ") == "viewer"

    async def test_legitimate_operator_passes_through(self) -> None:
        """A valid lower-cased role passes through unchanged."""
        assert await self._resolve_with_role("operator") == "operator"


# ---------------------------------------------------------------------------
# Finding #12: connector password scrub in /api/connect
# ---------------------------------------------------------------------------


class TestQuickConnectScrubsCredentials:
    """The plaintext password must not flow into the session record / audit log."""

    def test_scrub_helper_masks_sensitive_keys(self) -> None:
        from provide.uterm.server.routes.tunnels import _scrub_sensitive

        cleaned = _scrub_sensitive({"host": "h", "password": "p", "passphrase": "q", "secret": "s", "token": "t"})
        assert cleaned["host"] == "h"
        assert cleaned["password"] == "***"
        assert cleaned["passphrase"] == "***"
        assert cleaned["secret"] == "***"
        assert cleaned["token"] == "***"

    async def test_session_record_does_not_contain_plaintext_password(self) -> None:
        import json as _json

        from fastapi.testclient import TestClient

        from provide.uterm.server import create_server_app, default_server_config

        # Local connector — no real network needed.  The point is to assert
        # that the persisted session.connector_config and the response body
        # never reflect the plaintext password.
        cfg = default_server_config()
        cfg.auth.mode = "header"
        cfg.auth.header_mode_acknowledged = True
        cfg.auth.worker_bearer_token = "x" * 40
        app = create_server_app(cfg)
        client = TestClient(app)
        resp = client.post(
            "/api/connect",
            json={
                "connector_type": "shell",
                "display_name": "test",
                "command": "/bin/true",
                "password": "super-secret-pw",
            },
            headers={"x-uterm-principal": "alice", "x-uterm-role": "admin"},
        )
        assert resp.status_code in {200, 201}, resp.text
        body = resp.json()
        body_blob = _json.dumps(body)
        assert "super-secret-pw" not in body_blob
        sid = body["session_id"]
        registry = app.state.uterm_registry
        sd = await registry.get_definition(sid)
        assert sd is not None
        blob = _json.dumps(sd.connector_config)
        assert "super-secret-pw" not in blob
        # The scrub sentinel is present so the shape is preserved.
        assert sd.connector_config.get("password") == "***"
