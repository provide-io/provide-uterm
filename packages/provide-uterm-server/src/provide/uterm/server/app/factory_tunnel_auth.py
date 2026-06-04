#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tunnel share/worker principal resolution for the hosted terminal server.

These helpers resolve the request/websocket principal for tunnel *share* and
*worker* auth — the cookie-only share flow and the per-session worker token
flow.  They are pure functions of the incoming connection plus the server
config; ``factory_impl`` wires them into the ``_require_authenticated``
dependency.
"""

from __future__ import annotations

import secrets
import time
from typing import TYPE_CHECKING

from provide.telemetry import get_logger
from provide.uterm.server.auth import extract_bearer_token
from provide.uterm.server.bridge.identity import Principal

if TYPE_CHECKING:
    import re

    from starlette.requests import HTTPConnection

    from provide.uterm.server.models import ServerConfig

logger = get_logger(__name__)


def share_session_id_for(path: str, patterns: tuple[re.Pattern[str], ...]) -> str | None:
    """Return the session id a tunnel-share path maps to, or ``None``."""
    for pattern in patterns:
        match = pattern.match(path)
        if match is not None:
            return str(match.group("session_id"))
    return None


def resolve_tunnel_share_principal(
    connection: HTTPConnection,
    *,
    config: ServerConfig,
    patterns: tuple[re.Pattern[str], ...],
) -> Principal | None:
    """Resolve a viewer/operator share principal from the per-session cookie."""
    path = str(connection.scope.get("path", ""))
    session_id = share_session_id_for(path, patterns)
    if session_id is None:
        return None
    # Tunnel share/control auth is cookie-only after the one-time
    # ``?invite=`` bootstrap. Do not accept raw bearer tokens in the query
    # string; URLs are routinely logged by proxies and browser history.
    provided = None
    from http.cookies import SimpleCookie

    app = connection.scope.get("app")
    token_map = getattr(getattr(app, "state", object()), "uterm_tunnel_tokens", {})
    token_state = token_map.get(session_id) if isinstance(token_map, dict) else None
    if token_state is None:
        return None

    cookie_header = dict(connection.scope.get("headers", [])).get(b"cookie", b"").decode("utf-8", errors="ignore")
    cookies = SimpleCookie(cookie_header)
    cookie_key = f"uterm_tunnel_{session_id}"
    if cookie_key in cookies:
        provided = cookies[cookie_key].value
    if not provided:
        return None
    # Check expiry.
    expires_at = token_state.get("expires_at")
    if isinstance(expires_at, (int, float)) and time.time() > float(expires_at):
        logger.info("tunnel_token_expired session_id=%s", session_id)
        return None
    # Check IP binding.
    if config.tunnel.ip_binding:
        issued_ip = token_state.get("issued_ip")
        client_ip = str((connection.scope.get("client") or ("unknown", 0))[0])
        if issued_ip and issued_ip != client_ip:
            logger.info("tunnel_token_ip_mismatch session_id=%s issued=%s actual=%s", session_id, issued_ip, client_ip)
            return None
    # Match token type. The stored values are BLAKE2b digests, so we
    # compare the hash of the caller-supplied token against the stored
    # hash in constant time — see ``tunnel/token_hash.py``.
    from provide.uterm.tunnel.token_hash import verify_token

    source_ip = str((connection.scope.get("client") or ("unknown", 0))[0])
    if verify_token(str(provided), str(token_state.get("control_token_hash", ""))):
        connection.state.uterm_share_token = str(provided)
        connection.state.uterm_share_role = "operator"
        logger.info("tunnel_token_validated session_id=%s token_type=control source_ip=%s", session_id, source_ip)
        return Principal(
            subject_id=f"share:{session_id}:operator",
            roles=frozenset({"admin"}),
            scopes=frozenset({"*"}),
            # Confine the admin grant to this share's session: the operator
            # drives its own session with full admin capabilities but is not
            # a global administrator, so the grant cannot escalate to other
            # sessions even if this principal is resolved off-path.
            admin_session_scope=session_id,
        )
    if verify_token(str(provided), str(token_state.get("share_token_hash", ""))):
        connection.state.uterm_share_token = str(provided)
        connection.state.uterm_share_role = "viewer"
        logger.info("tunnel_token_validated session_id=%s token_type=share source_ip=%s", session_id, source_ip)
        return Principal(
            subject_id=f"share:{session_id}:viewer",
            roles=frozenset({"viewer"}),
            scopes=frozenset({"session.read"}),
        )
    logger.info("tunnel_token_validation_failed session_id=%s source_ip=%s", session_id, source_ip)
    return None


def resolve_tunnel_ws_worker_principal(
    connection: HTTPConnection,
    *,
    config: ServerConfig,
) -> Principal | None:
    """Resolve a worker principal for a ``/tunnel/{id}`` websocket upgrade."""
    path = str(connection.scope.get("path", ""))
    if not path.startswith("/tunnel/"):
        return None
    worker_id = path.removeprefix("/tunnel/")
    if not worker_id:  # pragma: no cover — FastAPI's path matcher already excludes the empty-id case
        return None

    provided = extract_bearer_token(connection.headers)
    if not provided:  # pragma: no cover — WS upgrade with no Authorization header already 401s upstream
        return None

    # Tunnel workers in JWT mode should still be able to authenticate with
    # the raw global worker token.  This keeps CLI/runtime behaviour
    # aligned with /ws/worker/ auth before JWT resolution is attempted.
    if config.auth.worker_bearer_token is not None and secrets.compare_digest(
        provided,
        config.auth.worker_bearer_token,
    ):
        connection.state.uterm_worker_token = provided
        return Principal(subject_id="worker", roles=frozenset({"admin"}), scopes=frozenset({"*"}))

    app = connection.scope.get("app")
    token_map = getattr(getattr(app, "state", object()), "uterm_tunnel_tokens", {})
    token_state = token_map.get(worker_id) if isinstance(token_map, dict) else None
    if not isinstance(token_state, dict):
        return None

    # Check expiry.
    expires_at = token_state.get("expires_at")
    if isinstance(expires_at, (int, float)) and time.time() > float(expires_at):
        logger.info("tunnel_token_expired session_id=%s", worker_id)
        return None

    # Check IP binding.
    if config.tunnel.ip_binding:
        issued_ip = token_state.get("issued_ip")
        client_ip = str((connection.scope.get("client") or ("unknown", 0))[0])
        if (
            issued_ip and issued_ip != client_ip
        ):  # pragma: no branch — matching-IP happy-path is covered by tunnel auth tests
            logger.info("tunnel_token_ip_mismatch session_id=%s issued=%s actual=%s", worker_id, issued_ip, client_ip)
            return None

    # Match token type (stored as BLAKE2b digest).
    from provide.uterm.tunnel.token_hash import verify_token

    source_ip = str((connection.scope.get("client") or ("unknown", 0))[0])
    if verify_token(str(provided), str(token_state.get("worker_token_hash", ""))):
        connection.state.uterm_worker_token = str(provided)
        logger.info("tunnel_worker_token_validated session_id=%s source_ip=%s", worker_id, source_ip)
        return Principal(subject_id="worker", roles=frozenset({"admin"}), scopes=frozenset({"*"}))

    logger.info("tunnel_worker_token_validation_failed session_id=%s source_ip=%s", worker_id, source_ip)
    return None
