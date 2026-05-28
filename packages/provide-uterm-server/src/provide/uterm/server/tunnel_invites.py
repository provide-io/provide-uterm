#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""One-time tunnel invite bootstrap helpers."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Literal

from provide.uterm.tunnel.token_hash import hash_token, verify_token

TunnelInviteRole = Literal["viewer", "operator"]
INVITE_TTL_S = 300


@dataclass(frozen=True, slots=True)
class TunnelInvite:
    session_id: str
    role: TunnelInviteRole
    tunnel_token: str
    expires_at: float
    issued_ip: str | None = None


def issue_tunnel_invites(
    invite_store: dict[str, dict[str, object]],
    *,
    session_id: str,
    share_token: str,
    control_token: str,
    tunnel_expires_at: float,
    issued_ip: str | None,
    now: float | None = None,
) -> tuple[str, str]:
    """Create one-time viewer/operator invites and store only hashed invite keys."""
    now_ts = time.time() if now is None else now
    invite_expires_at = min(float(tunnel_expires_at), now_ts + INVITE_TTL_S)
    share_invite = secrets.token_urlsafe(32)
    control_invite = secrets.token_urlsafe(32)
    invite_store[hash_token(share_invite)] = {
        "session_id": session_id,
        "role": "viewer",
        "tunnel_token": share_token,
        "expires_at": invite_expires_at,
        "issued_ip": issued_ip,
    }
    invite_store[hash_token(control_invite)] = {
        "session_id": session_id,
        "role": "operator",
        "tunnel_token": control_token,
        "expires_at": invite_expires_at,
        "issued_ip": issued_ip,
    }
    return share_invite, control_invite


def consume_tunnel_invite(
    invite_store: dict[str, dict[str, object]],
    invite: str,
    *,
    session_id: str,
    now: float | None = None,
) -> TunnelInvite | None:
    """Consume and return a matching invite, or ``None`` if it is invalid."""
    invite_value = str(invite or "").strip()
    if not invite_value:
        return None
    invite_hash = hash_token(invite_value)
    raw = invite_store.pop(invite_hash, None)
    if raw is None:
        return None
    now_ts = time.time() if now is None else now
    expires_at = raw.get("expires_at")
    if not isinstance(expires_at, (int, float)) or now_ts > float(expires_at):
        return None
    if str(raw.get("session_id", "")) != session_id:
        return None
    role = str(raw.get("role", ""))
    if role not in {"viewer", "operator"}:
        return None
    tunnel_token = str(raw.get("tunnel_token", "")).strip()
    if not tunnel_token:
        return None
    issued_ip = raw.get("issued_ip")
    return TunnelInvite(
        session_id=session_id,
        role=role,  # type: ignore[arg-type]
        tunnel_token=tunnel_token,
        expires_at=float(expires_at),
        issued_ip=str(issued_ip) if issued_ip is not None else None,
    )


def discard_tunnel_invites_for_session(invite_store: dict[str, dict[str, object]], session_id: str) -> None:
    """Remove all pending invites for *session_id*."""
    stale = [key for key, value in invite_store.items() if str(value.get("session_id", "")) == session_id]
    for key in stale:
        invite_store.pop(key, None)


def sweep_expired_tunnel_invites(invite_store: dict[str, dict[str, object]], *, now: float | None = None) -> None:
    now_ts = time.time() if now is None else now
    expired: list[str] = []
    for key, value in invite_store.items():
        expires_at = value.get("expires_at")
        if isinstance(expires_at, (int, float)) and now_ts > float(expires_at):
            expired.append(key)
    for key in expired:
        invite_store.pop(key, None)


def tunnel_invite_matches_token_hash(invite: TunnelInvite, token_hash: str) -> bool:
    """Return True when the consumed invite still matches the active tunnel token."""
    return verify_token(invite.tunnel_token, token_hash)
