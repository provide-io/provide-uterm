#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Stub identity provider for the ``dev_token`` auth mode.

Generates an HS256 shared secret at startup, mints a short-lived JWT
with an admin-role claim, writes the token to a 0600 file under
``~/.cache/uterm/dev_token``, and mutates the passed-in :class:`AuthConfig`
so the regular JWT validator handles incoming requests.

This replaces the previous ``mode in {"none","dev"}`` short-circuit which
disabled authentication entirely and let any caller claim any principal
via ``X-Principal`` / ``X-Role`` headers — a real bypass for anything
on loopback (containers, sidecars, ssh tunnels). The new path keeps the
"single-config-knob ergonomics" of dev mode while routing through the
same JWT codepath production does.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from provide.uterm.server.models import AuthConfig

logger = logging.getLogger(__name__)

# Default location for the auto-issued dev token. Lives under the user's
# cache directory so it survives sessions but is never committed.
DEFAULT_DEV_TOKEN_PATH: Path = Path.home() / ".cache" / "uterm" / "dev_token"

# Lifetime of the auto-issued token. Long enough to survive a workday of
# interactive use; short enough that an exfiltrated file goes stale quickly.
DEV_TOKEN_TTL_S: int = 24 * 3600


def setup_dev_idp(
    auth: AuthConfig,
    *,
    token_path: Path | None = None,
    subject: str = "dev-user",
    roles: tuple[str, ...] = ("admin",),
    ttl_s: int = DEV_TOKEN_TTL_S,
) -> str:
    """Configure ``auth`` for ``dev_token`` mode and return the issued JWT.

    Side effects:
        - Generates a fresh HS256 shared secret (cryptographically random,
          ≥256 bits).
        - Mutates ``auth`` so the regular JWT validator accepts the new
          secret. ``auth.mode`` is set to ``"jwt"`` so all downstream
          checks (entropy validation, audience/issuer enforcement) run.
        - Mints a JWT with ``sub=subject`` and ``roles`` claim.
        - Writes the token to ``token_path`` (default
          ``~/.cache/uterm/dev_token``) with mode 0600.

    Returns the plain token. In-process callers (test fixtures, the
    demo recorder) can use it as a Bearer token. CLI consumers should
    read the file.
    """
    try:
        import jwt
    except ImportError as exc:  # pragma: no cover — required dep
        raise RuntimeError("pyjwt is required for dev_token mode") from exc

    secret = secrets.token_urlsafe(48)  # ~384 bits, well above the 32-char floor
    auth.mode = "jwt"
    auth.jwt_public_key_pem = secret
    auth.jwt_algorithms = ["HS256"]
    auth.jwt_issuer = auth.jwt_issuer or "provide-uterm-dev"
    auth.jwt_audience = auth.jwt_audience or "provide-uterm-server"
    # The worker bearer token is independent from the IdP signing key but
    # also needs to meet the 32-char floor in production-like configs.
    if not auth.worker_bearer_token:
        auth.worker_bearer_token = secrets.token_urlsafe(32)

    now = int(time.time())
    token = jwt.encode(
        {
            "sub": subject,
            "iss": auth.jwt_issuer,
            "aud": auth.jwt_audience,
            "iat": now,
            "exp": now + ttl_s,
            auth.jwt_roles_claim: list(roles),
        },
        secret,
        algorithm="HS256",
    )

    path = token_path or DEFAULT_DEV_TOKEN_PATH
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(token)
    # On macOS / Linux Path.write_text doesn't set mode; chmod explicitly.
    # Windows ignores POSIX modes; the .cache directory ACL is the only
    # guard there. Same posture as the existing tunnel-token file flows.
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover — best-effort on non-POSIX
        pass

    logger.info(
        "dev_idp_token_issued path=%s subject=%s ttl_s=%d roles=%s",
        path,
        subject,
        ttl_s,
        list(roles),
    )
    return token


def read_dev_token(token_path: Path | None = None) -> str | None:
    """Return the last-issued dev token from disk, or ``None`` if absent.

    CLI tools and out-of-process clients use this to pick up a token
    that the server's ``setup_dev_idp`` minted at startup.
    """
    path = token_path or DEFAULT_DEV_TOKEN_PATH
    try:
        return path.read_text().strip() or None
    except OSError:
        return None
