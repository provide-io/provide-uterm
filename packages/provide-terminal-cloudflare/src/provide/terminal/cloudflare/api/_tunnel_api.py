#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tunnel API helpers for the Default Worker entry point."""

from __future__ import annotations

import json
import secrets
import time
import uuid
from urllib.parse import parse_qs, urlparse

try:
    from provide.terminal.cloudflare.cf_types import Response, json_response
except ImportError:  # pragma: no cover
    from cf_types import Response, json_response  # type: ignore[import-not-found]


async def handle_tunnels(request: object, env: object, principal: object | None = None) -> object:
    """Handle POST /api/tunnels — create a tunnel session with share tokens.

    When ``principal`` is provided (JWT mode with authenticated caller),
    the tunnel is created with ``owner=principal.subject_id`` and
    ``visibility="private"``.  Tunnels authenticate their guests via
    bearer-capability URLs (share/control tokens), so the session itself
    should NOT be publicly listable — otherwise any authenticated caller
    can discover it through the normal fleet-listing API.
    """
    method = str(getattr(request, "method", "GET")).upper()
    if method != "POST":
        return json_response({"error": "method not allowed"}, status=405)
    try:
        raw = await request.json()  # type: ignore[union-attr]
        body = raw.to_py() if hasattr(raw, "to_py") else raw
    except Exception:
        body = {}
    tunnel_type = str(body.get("tunnel_type", "terminal"))
    display_name = str(body.get("display_name") or "")
    tunnel_id = f"tunnel-{uuid.uuid4().hex[:12]}"
    if not display_name:
        display_name = tunnel_id

    worker_token = secrets.token_urlsafe(32)
    share_token = secrets.token_urlsafe(32)
    control_token = secrets.token_urlsafe(32)

    now = time.time()
    ttl_s = int(body.get("ttl_s", 3600))
    ttl_s = max(60, min(ttl_s, 86400))
    expires_at = now + ttl_s

    owner = getattr(principal, "subject_id", None) if principal is not None else None
    visibility = "private" if owner is not None else "public"

    entry = {
        "session_id": tunnel_id,
        "display_name": display_name,
        "created_at": now,
        "expires_at": expires_at,
        "connector_type": f"tunnel:{tunnel_type}",
        "lifecycle_state": "waiting",
        "input_mode": "open",
        "connected": False,
        "auto_start": False,
        "tags": list(body.get("tags") or []),
        "recording_enabled": True,
        "recording_available": False,
        "owner": owner,
        "visibility": visibility,
        "last_error": None,
        "hijacked": False,
        "tunnel_type": tunnel_type,
        "worker_token": worker_token,
        "share_token": share_token,
        "control_token": control_token,
        "issued_ip": str(getattr(request, "headers", {}).get("CF-Connecting-IP") or ""),
        "share_page": "inspect" if tunnel_type == "http" else "session",
    }
    kv = getattr(env, "SESSION_REGISTRY", None)
    if kv is not None:
        await kv.put(f"session:{tunnel_id}", json.dumps(entry))

    base_url = str(getattr(request, "url", "")).split("/api/")[0]
    # Mirrored in server/routes/api.py — keep both in sync.
    share_page = "inspect" if tunnel_type == "http" else "session"
    return json_response(
        {
            "tunnel_id": tunnel_id,
            "display_name": display_name,
            "tunnel_type": tunnel_type,
            "ws_endpoint": f"/tunnel/{tunnel_id}",
            "worker_token": worker_token,
            "share_url": f"{base_url}/app/{share_page}/{tunnel_id}?token={share_token}",
            "control_url": f"{base_url}/app/operator/{tunnel_id}?token={control_token}",
            "expires_at": expires_at,
        }
    )


def _principal_can_manage_tunnel(principal: object | None, entry: dict) -> bool:
    """Return True if ``principal`` is admin or the tunnel's owner.

    ``principal=None`` (open-access / none/dev mode) is permitted so that
    local development without JWT config keeps working.  In JWT mode a
    non-None principal must pass ownership or admin-role check.
    """
    if principal is None:
        return True
    roles = tuple(getattr(principal, "roles", ()) or ())
    if "admin" in roles:
        return True
    subject_id = getattr(principal, "subject_id", None)
    owner = entry.get("owner")
    return owner is not None and subject_id == owner


async def handle_tunnel_revoke_tokens(
    _request: object, env: object, tunnel_id: str, principal: object | None = None
) -> object:
    """Handle DELETE /api/tunnels/{id}/tokens — revoke all tokens.

    Owner or admin only.  Without this check any authenticated caller
    could revoke someone else's tunnel (the reviewer reproduced this
    with a CF Access service token against an owned tunnel).
    """
    kv = getattr(env, "SESSION_REGISTRY", None)
    if kv is None:
        return json_response({"error": "SESSION_REGISTRY not configured"}, status=500)
    raw = await kv.get(f"session:{tunnel_id}")
    if raw is None:
        return json_response({"error": "not found"}, status=404)
    try:
        entry = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError):
        return json_response({"error": "corrupt entry"}, status=500)
    if not _principal_can_manage_tunnel(principal, entry):
        return json_response({"error": "forbidden"}, status=403)
    entry["worker_token"] = None
    entry["share_token"] = None
    entry["control_token"] = None
    entry["revoked"] = True
    await kv.put(f"session:{tunnel_id}", json.dumps(entry))
    return json_response({"ok": True, "session_id": tunnel_id})


async def handle_tunnel_rotate_tokens(
    request: object,
    env: object,
    tunnel_id: str,
    principal: object | None = None,
    *,
    ttl_s: int = 3600,
) -> object:
    """Handle POST /api/tunnels/{id}/tokens/rotate — generate new tokens.

    Owner or admin only (same justification as revoke above).
    """
    kv = getattr(env, "SESSION_REGISTRY", None)
    if kv is None:
        return json_response({"error": "SESSION_REGISTRY not configured"}, status=500)
    raw = await kv.get(f"session:{tunnel_id}")
    if raw is None:
        return json_response({"error": "not found"}, status=404)
    try:
        entry = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError):
        return json_response({"error": "corrupt entry"}, status=500)
    if not _principal_can_manage_tunnel(principal, entry):
        return json_response({"error": "forbidden"}, status=403)

    new_worker = secrets.token_urlsafe(32)
    new_share = secrets.token_urlsafe(32)
    new_control = secrets.token_urlsafe(32)
    expires_at = time.time() + ttl_s

    entry["worker_token"] = new_worker
    entry["share_token"] = new_share
    entry["control_token"] = new_control
    entry["expires_at"] = expires_at
    entry.pop("revoked", None)  # rotation re-enables a revoked tunnel
    tunnel_type = str(entry.get("tunnel_type") or "terminal")
    # Mirrored in server/routes/api.py — keep both in sync.
    share_page = "inspect" if tunnel_type == "http" else "session"
    entry["share_page"] = share_page
    await kv.put(f"session:{tunnel_id}", json.dumps(entry))

    base_url = str(getattr(request, "url", "")).split("/api/")[0]
    return json_response(
        {
            "tunnel_id": tunnel_id,
            "ws_endpoint": f"/tunnel/{tunnel_id}",
            "worker_token": new_worker,
            "share_url": f"{base_url}/app/{share_page}/{tunnel_id}?token={new_share}",
            "control_url": f"{base_url}/app/operator/{tunnel_id}?token={new_control}",
            "expires_at": expires_at,
        }
    )


async def resolve_share_context(
    request: object, env: object, tunnel_id: str, config: object = None
) -> tuple[str, str] | None:
    """Return ``(page_kind, share_role)`` for a valid share token.

    ``config`` is optional: when supplied, ``tunnel_token_transport`` and
    ``tunnel_ip_binding`` are enforced.  Without it (e.g. legacy call-sites
    in tests) the defaults are ``transport="both"`` and ``ip_binding=False``.
    """
    kv = getattr(env, "SESSION_REGISTRY", None)
    if kv is None:
        return None
    raw = await kv.get(f"session:{tunnel_id}")
    if raw is None:
        return None
    try:
        session = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError):
        return None

    # Explicit revocation: handle_tunnel_revoke_tokens sets this flag.  Without
    # the check the implicit-viewer branch below would grant access when both
    # tokens are None (the post-revocation state).
    if session.get("revoked"):
        return None

    share_tok = session.get("share_token")
    control_tok = session.get("control_token")

    # Effective transport mode and IP-binding from config (defaults: both / off).
    transport = "both"
    ip_binding = False
    if config is not None:
        transport = str(getattr(config, "tunnel_token_transport", "both"))
        ip_binding = bool(getattr(config, "tunnel_ip_binding", False))

    provided = None
    if transport != "cookie":  # "query" or "both": try query string first
        try:
            qs = parse_qs(urlparse(str(request.url)).query)  # type: ignore[attr-defined]
            provided = (qs.get("token", [None]) or [None])[0]
        except Exception:  # noqa: S110
            pass
    # Cookie fallback: uterm_tunnel_{tunnel_id}
    if not provided and transport != "query":  # "cookie" or "both"
        try:
            from http.cookies import SimpleCookie

            cookie_header = str(
                getattr(request, "headers", {}).get("cookie") or getattr(request, "headers", {}).get("Cookie") or ""
            )
            cookies = SimpleCookie(cookie_header)
            cookie_key = f"uterm_tunnel_{tunnel_id}"
            if cookie_key in cookies:
                provided = cookies[cookie_key].value
        except Exception:  # noqa: S110
            pass

    # Check expiry.
    expires_at = session.get("expires_at")
    if isinstance(expires_at, (int, float)) and time.time() > float(expires_at):
        return None

    # Timing-safe comparison — prevents brute-force via response timing.
    role: str | None = None
    if control_tok and provided and secrets.compare_digest(str(provided), str(control_tok)):
        role = "operator"
    elif (share_tok and provided and secrets.compare_digest(str(provided), str(share_tok))) or (
        not share_tok and not control_tok
    ):
        role = "viewer"

    if role is None:
        return None

    # IP binding: a valid token from the wrong origin IP is rejected.
    if ip_binding:
        issued_ip = session.get("issued_ip") or ""
        client_ip = ""
        try:
            headers = getattr(request, "headers", {})
            client_ip = str(headers.get("CF-Connecting-IP") or headers.get("cf-connecting-ip") or "")
        except Exception:  # noqa: S110
            pass
        if issued_ip and client_ip != issued_ip:
            return None

    return ("operator" if role == "operator" else "session", role)


async def handle_share_route(
    request: object,
    env: object,
    tunnel_id: str,
    spa_response: object,
) -> Response:
    """Serve a shared tunnel page when the presented token is valid."""
    share_context = await resolve_share_context(request, env, tunnel_id)
    if share_context is None:
        # Return 404 for both "not found" and "invalid token" to prevent enumeration.
        return json_response({"error": "not_found", "session_id": tunnel_id}, status=404)

    page_kind, share_role = share_context
    query = parse_qs(urlparse(str(request.url)).query)  # type: ignore[attr-defined]
    token = ((query.get("token", []) + query.get("access_token", [])) or [None])[0]
    return spa_response(
        page_kind,
        session_id=tunnel_id,
        surface="operator" if share_role == "operator" else "user",
        share_role=share_role,
        share_token=token,
    )
