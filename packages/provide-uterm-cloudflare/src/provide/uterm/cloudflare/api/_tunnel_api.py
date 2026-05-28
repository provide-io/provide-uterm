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
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from provide.uterm.tunnel.token_hash import hash_token, verify_token

if TYPE_CHECKING:
    from collections.abc import Callable

    from provide.uterm.cloudflare.cf_types import Response, json_response
else:
    try:
        from provide.uterm.cloudflare.cf_types import Response, json_response
    except ImportError:  # pragma: no cover
        from cf_types import Response, json_response  # type: ignore[import-not-found,no-redef]

_TUNNEL_INVITE_TTL_S = 300


def _issue_tunnel_invites(
    entry: dict[str, Any],
    *,
    share_token: str,
    control_token: str,
    now: float,
) -> tuple[str, str]:
    share_invite = secrets.token_urlsafe(32)
    control_invite = secrets.token_urlsafe(32)
    expires_at = now + _TUNNEL_INVITE_TTL_S
    entry.update(
        {
            "share_invite_hash": hash_token(share_invite),
            "share_invite_token": share_token,
            "share_invite_expires_at": expires_at,
            "control_invite_hash": hash_token(control_invite),
            "control_invite_token": control_token,
            "control_invite_expires_at": expires_at,
        }
    )
    return share_invite, control_invite


def _clear_tunnel_invites(entry: dict[str, Any]) -> None:
    for key in (
        "share_invite_hash",
        "share_invite_token",
        "share_invite_expires_at",
        "control_invite_hash",
        "control_invite_token",
        "control_invite_expires_at",
    ):
        entry.pop(key, None)


def _clear_tunnel_invite(entry: dict[str, Any], role: str) -> None:
    prefix = "control" if role == "operator" else "share"
    for suffix in ("hash", "token", "expires_at"):
        entry.pop(f"{prefix}_invite_{suffix}", None)


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
        raw = await request.json()  # type: ignore[attr-defined]
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

    # Tokens are stored as BLAKE2b digests; the plain values leave this
    # function only in the response below. KV storage is at-rest-encrypted
    # but disclosure of a KV dump (debugging, leaked credentials, mis-issued
    # access) would otherwise expose every active bearer in plaintext.
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
        "worker_token_hash": hash_token(worker_token),
        "share_token_hash": hash_token(share_token),
        "control_token_hash": hash_token(control_token),
        "issued_ip": str(getattr(request, "headers", {}).get("CF-Connecting-IP") or ""),
        "share_page": "inspect" if tunnel_type == "http" else "session",
    }
    share_invite, control_invite = _issue_tunnel_invites(
        entry,
        share_token=share_token,
        control_token=control_token,
        now=now,
    )
    kv = getattr(env, "SESSION_REGISTRY", None)
    if kv is not None:
        await kv.put(f"session:{tunnel_id}", json.dumps(entry))

    base_url = str(getattr(request, "url", "")).split("/api/")[0]
    return json_response(
        {
            "tunnel_id": tunnel_id,
            "display_name": display_name,
            "tunnel_type": tunnel_type,
            "ws_endpoint": f"/tunnel/{tunnel_id}",
            "worker_token": worker_token,
            "share_url": f"{base_url}/s/{tunnel_id}?invite={share_invite}",
            "control_url": f"{base_url}/s/{tunnel_id}?invite={control_invite}",
            "expires_at": expires_at,
        }
    )


def _principal_can_manage_tunnel(principal: object | None, entry: dict[str, Any]) -> bool:
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
    try:
        raw = await kv.get(f"session:{tunnel_id}")
    except Exception:
        return None
    if raw is None:
        return json_response({"error": "not found"}, status=404)
    try:
        entry = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError):
        return json_response({"error": "corrupt entry"}, status=500)
    if not _principal_can_manage_tunnel(principal, entry):
        return json_response({"error": "forbidden"}, status=403)
    entry["worker_token_hash"] = None
    entry["share_token_hash"] = None
    entry["control_token_hash"] = None
    entry["revoked"] = True
    _clear_tunnel_invites(entry)
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
    try:
        raw = await kv.get(f"session:{tunnel_id}")
    except Exception:
        return None
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

    entry["worker_token_hash"] = hash_token(new_worker)
    entry["share_token_hash"] = hash_token(new_share)
    entry["control_token_hash"] = hash_token(new_control)
    entry["expires_at"] = expires_at
    entry.pop("revoked", None)  # rotation re-enables a revoked tunnel
    tunnel_type = str(entry.get("tunnel_type") or "terminal")
    # Mirrored in server/routes/api.py — keep both in sync.
    share_page = "inspect" if tunnel_type == "http" else "session"
    entry["share_page"] = share_page
    share_invite, control_invite = _issue_tunnel_invites(
        entry,
        share_token=new_share,
        control_token=new_control,
        now=time.time(),
    )
    await kv.put(f"session:{tunnel_id}", json.dumps(entry))

    base_url = str(getattr(request, "url", "")).split("/api/")[0]
    return json_response(
        {
            "tunnel_id": tunnel_id,
            "ws_endpoint": f"/tunnel/{tunnel_id}",
            "worker_token": new_worker,
            "share_url": f"{base_url}/s/{tunnel_id}?invite={share_invite}",
            "control_url": f"{base_url}/s/{tunnel_id}?invite={control_invite}",
            "expires_at": expires_at,
        }
    )


async def resolve_share_context(
    request: object, env: object, tunnel_id: str, config: object = None
) -> tuple[str, str] | None:
    """Return ``(page_kind, share_role)`` for a valid share-token cookie."""
    kv = getattr(env, "SESSION_REGISTRY", None)
    if kv is None:
        return None
    try:
        raw = await kv.get(f"session:{tunnel_id}")
    except Exception:
        return None
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

    share_tok_hash = session.get("share_token_hash") or ""
    control_tok_hash = session.get("control_token_hash") or ""

    ip_binding = False
    if config is not None:
        ip_binding = bool(getattr(config, "tunnel_ip_binding", False))

    provided = None
    try:
        from http.cookies import SimpleCookie

        cookie_header = str(
            getattr(request, "headers", {}).get("cookie") or getattr(request, "headers", {}).get("Cookie") or ""
        )
        cookies = SimpleCookie(cookie_header)
        cookie_key = f"uterm_tunnel_{tunnel_id}"
        if cookie_key in cookies:
            provided = cookies[cookie_key].value
    except Exception:
        pass

    # Check expiry.
    expires_at = session.get("expires_at")
    if isinstance(expires_at, (int, float)) and time.time() > float(expires_at):
        return None

    # Constant-time hash compare; tokens are stored as BLAKE2b digests.
    role: str | None = None
    if provided and verify_token(str(provided), str(control_tok_hash)):
        role = "operator"
    elif provided and verify_token(str(provided), str(share_tok_hash)):
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
        except Exception:
            pass
        if issued_ip and client_ip != issued_ip:
            return None

    if role == "operator":
        return ("operator", role)
    return (str(session.get("share_page") or "session"), role)


async def consume_tunnel_invite(request: object, env: object, tunnel_id: str) -> tuple[str, str, str] | None:
    """Consume a one-time tunnel invite and return ``(page_kind, role, token)``."""
    try:
        query = parse_qs(urlparse(str(request.url)).query)  # type: ignore[attr-defined]
        invite = (query.get("invite", [None]) or [None])[0]
    except Exception:
        invite = None
    if not invite:
        return None

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
    if session.get("revoked"):
        return None

    expires_at = session.get("expires_at")
    now = time.time()
    if isinstance(expires_at, (int, float)) and now > float(expires_at):
        return None

    matched: tuple[str, str, str] | None = None
    for role, token_hash_key, invite_hash_key, invite_token_key, invite_expires_key in (
        ("operator", "control_token_hash", "control_invite_hash", "control_invite_token", "control_invite_expires_at"),
        ("viewer", "share_token_hash", "share_invite_hash", "share_invite_token", "share_invite_expires_at"),
    ):
        invite_hash = str(session.get(invite_hash_key) or "")
        raw_token = str(session.get(invite_token_key) or "")
        active_token_hash = str(session.get(token_hash_key) or "")
        invite_expires = session.get(invite_expires_key)
        if not invite_hash or not raw_token or not active_token_hash:
            continue
        if isinstance(invite_expires, (int, float)) and now > float(invite_expires):
            _clear_tunnel_invite(session, role)
            continue
        if verify_token(str(invite), invite_hash) and verify_token(raw_token, active_token_hash):
            page_kind = "operator" if role == "operator" else str(session.get("share_page") or "session")
            matched = (page_kind, role, raw_token)
            _clear_tunnel_invite(session, role)
            break

    if matched is not None:
        await kv.put(f"session:{tunnel_id}", json.dumps(session))
    return matched


async def handle_share_route(
    request: object,
    env: object,
    tunnel_id: str,
    spa_response: Callable[..., Response],
) -> Response:
    """Serve a shared tunnel page when the presented token is valid."""
    share_context = await resolve_share_context(request, env, tunnel_id)
    if share_context is None:
        # Return 404 for both "not found" and "invalid token" to prevent enumeration.
        return json_response({"error": "not_found", "session_id": tunnel_id}, status=404)

    page_kind, share_role = share_context
    return spa_response(
        page_kind,
        session_id=tunnel_id,
        surface="operator" if share_role == "operator" else "user",
        share_role=share_role,
    )
