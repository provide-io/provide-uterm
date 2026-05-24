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

# Minimum string length for a production-like bearer token or HMAC shared
# secret. 32 chars is the lowest floor that survives all common encodings
# while still corresponding to ≥128 bits of entropy:
#   * raw 32-byte binary token (256 bits)
#   * 16-byte hex-encoded token (128 bits)
#   * 24-byte base64-encoded token (~192 bits)
# RFC 8725 §3.5 recommends ≥256 bits for HS256 keys; the check guards the
# common deployment mistake of "I'll use 'secret' temporarily", not the
# malicious-but-knowledgeable operator.
_MIN_BEARER_TOKEN_CHARS = 32

# Heuristic for "this string is an HMAC shared secret, not a PEM public
# key": PEM blobs always start with the standard armour marker. An HS256
# secret can be any other shape (raw bytes / hex / base64 / urlsafe).
_PEM_PREFIX = "-----BEGIN"


def _is_loopback_host(host: str) -> bool:
    return str(host).strip().lower() in _LOOPBACK_HOSTS


def _is_placeholder_auth_value(value: str | None) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    if text in _PLACEHOLDER_AUTH_VALUES:
        return True
    return any(marker in text for marker in _PLACEHOLDER_AUTH_MARKERS)


def _is_low_entropy_bearer(value: str | None) -> bool:
    """True if ``value`` is suspiciously weak for a production bearer token.

    Checks raw character length only — base64 / hex / raw bytes all admit
    a uniform ≥32-char floor that maps to ≥128 bits of attacker work.
    """
    text = str(value or "")
    return 0 < len(text) < _MIN_BEARER_TOKEN_CHARS


def _is_low_entropy_hmac_secret(value: str | None, algorithms: tuple[str, ...] | list[str]) -> bool:
    """True iff ``value`` is meant as an HMAC shared secret and is too short.

    Returns False for PEM-encoded public keys (which are long by
    construction) or when no HMAC algorithm is configured.
    """
    text = str(value or "").strip()
    if not text or text.startswith(_PEM_PREFIX):
        return False
    if not any(str(a).strip().upper().startswith("HS") for a in algorithms):
        return False
    return len(text) < _MIN_BEARER_TOKEN_CHARS


def _validate_no_placeholder_auth_values(config: ServerConfig, mode: str) -> None:
    production_like = config.auth.require_jwt_in_production or not _is_loopback_host(config.server.host)
    if not production_like:
        return

    if _is_placeholder_auth_value(config.auth.worker_bearer_token):
        raise ValueError(
            f"auth.worker_bearer_token uses a known placeholder value when auth.mode='{mode}'. "
            "Set a high-entropy runtime token."
        )
    if _is_low_entropy_bearer(config.auth.worker_bearer_token):
        raise ValueError(
            f"auth.worker_bearer_token is shorter than {_MIN_BEARER_TOKEN_CHARS} characters when "
            f"auth.mode='{mode}' on a non-loopback host. Use at least 32 chars of high-entropy "
            "material (e.g. `python -c 'import secrets; print(secrets.token_urlsafe(32))'`)."
        )

    if mode == "jwt" and _is_placeholder_auth_value(config.auth.jwt_public_key_pem):
        raise ValueError(
            "auth.jwt_public_key_pem uses a known placeholder value when auth.mode='jwt'. "
            "Set a real JWT public key or HS256 shared secret."
        )
    if mode == "jwt" and _is_low_entropy_hmac_secret(config.auth.jwt_public_key_pem, config.auth.jwt_algorithms):
        raise ValueError(
            f"auth.jwt_public_key_pem is an HMAC shared secret shorter than {_MIN_BEARER_TOKEN_CHARS} "
            "characters. RFC 8725 §3.5 requires HS256 keys ≥256 bits — use a longer secret or switch "
            "to an asymmetric algorithm (RS256/ES256)."
        )


def _validate_auth_config(config: ServerConfig) -> None:
    mode = str(config.auth.mode).strip().lower()
    if mode == "dev_token":
        # Stub-IdP path: mints an HS256 key + JWT at startup, writes the
        # token to a 0600 file, and rewrites config.auth so the regular
        # JWT validator runs. Auth code paths collapse to one — no
        # X-Principal/X-Role bypass exists in this mode. This is the
        # safe replacement for ``dev``/``none``; those legacy modes
        # remain available below until tests migrate.
        from provide.uterm.server.dev_idp import setup_dev_idp

        host = str(config.server.host).strip().lower()
        if not _is_loopback_host(host):
            raise RuntimeError(
                "auth.mode='dev_token' is only permitted when server.host is a loopback address "
                f"(127.0.0.1, localhost, or ::1). Got: {host}"
            )
        setup_dev_idp(config.auth)
        # setup_dev_idp mutated mode → "jwt"; fall through to jwt validation.
        mode = str(config.auth.mode).strip().lower()
    if mode in {"none", "dev"}:
        if config.auth.require_jwt_in_production:
            raise RuntimeError(
                f"auth.mode='{mode}' is not allowed when auth.require_jwt_in_production=true. "
                "Set auth.mode='jwt' or 'dev_token', or disable require_jwt_in_production."
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
            "Do NOT expose this server on a public network in this mode. "
            "Use auth.mode='dev_token' instead — same single-knob ergonomics with auto-issued JWT.",
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
