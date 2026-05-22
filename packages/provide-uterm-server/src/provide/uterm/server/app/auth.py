#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Auth-config validation for the hosted terminal server."""

from __future__ import annotations

from typing import TYPE_CHECKING

from provide.telemetry import get_logger

if TYPE_CHECKING:
    from provide.uterm.server.models import ServerConfig

logger = get_logger(__name__)

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

_PLACEHOLDER_AUTH_VALUES = {
    "key",
    "token",
    "secret",
    "secret123",
    "dummy-token",
    "dummy-secret",
    "worker-token",
    "worker-secret",
    "jwt-secret",
    "jwt-secret-key",
}

_PLACEHOLDER_AUTH_MARKERS = (
    "change-me",
    "changeme",
    "placeholder",
    "replace-me",
    "replace_with",
    "replace-with",
)


def _is_loopback_host(host: str) -> bool:
    return str(host).strip().lower() in _LOOPBACK_HOSTS


def _is_placeholder_auth_value(value: str | None) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    if text in _PLACEHOLDER_AUTH_VALUES:
        return True
    return any(marker in text for marker in _PLACEHOLDER_AUTH_MARKERS)


def _validate_no_placeholder_auth_values(config: ServerConfig, mode: str) -> None:
    production_like = config.auth.require_jwt_in_production or not _is_loopback_host(config.server.host)
    if not production_like:
        return

    if _is_placeholder_auth_value(config.auth.worker_bearer_token):
        raise ValueError(
            f"auth.worker_bearer_token uses a known placeholder value when auth.mode='{mode}'. "
            "Set a high-entropy runtime token."
        )

    if mode == "jwt" and _is_placeholder_auth_value(config.auth.jwt_public_key_pem):
        raise ValueError(
            "auth.jwt_public_key_pem uses a known placeholder value when auth.mode='jwt'. "
            "Set a real JWT public key or HS256 shared secret."
        )


def _validate_auth_config(config: ServerConfig) -> None:
    mode = str(config.auth.mode).strip().lower()
    if mode in {"none", "dev"}:
        if config.auth.require_jwt_in_production:
            raise RuntimeError(
                f"auth.mode='{mode}' is not allowed when auth.require_jwt_in_production=true. "
                "Set auth.mode='jwt' or disable require_jwt_in_production."
            )

        host = str(config.server.host).strip().lower()
        if not _is_loopback_host(host):
            raise RuntimeError(
                f"auth.mode='{mode}' is only permitted when server.host is a loopback address "
                f"(127.0.0.1, localhost, or ::1). Got: {host}"
            )

        # Warn loudly — in dev/none mode any request can spoof any principal
        # via the X-Principal/X-Role headers.  Never expose this mode publicly.
        logger.warning(
            "auth_mode=%s: authentication is disabled — any caller can claim any identity. "
            "Do NOT expose this server on a public network in this mode.",
            mode,
        )
        return
    if mode == "header":
        if not config.auth.header_mode_acknowledged:
            raise ValueError(
                "auth.mode='header' requires auth.header_mode_acknowledged=true. "
                "This mode trusts X-Principal/X-Role from all callers — only safe behind a reverse proxy."
            )
        logger.warning(
            "auth_mode=header: trusting X-Principal/X-Role headers from all callers. "
            "This mode MUST run behind a reverse proxy that sets these headers. "
            "Direct exposure allows any client to claim any identity.",
        )
    # All authenticated modes (jwt, header, …) require a worker bearer token.
    if not config.auth.worker_bearer_token:
        raise ValueError(f"auth.worker_bearer_token is required when auth.mode='{mode}'")
    _validate_no_placeholder_auth_values(config, mode)
    if mode != "jwt":
        return
    if not config.auth.jwt_algorithms:
        raise ValueError("auth.jwt_algorithms must not be empty when auth.mode='jwt'")
    if any(a.strip().lower() == "none" for a in config.auth.jwt_algorithms):
        raise ValueError("'none' is not permitted in auth.jwt_algorithms")
    if not config.auth.jwt_public_key_pem and not config.auth.jwt_jwks_url:
        raise ValueError("configure auth.jwt_public_key_pem or auth.jwt_jwks_url when auth.mode='jwt'")
