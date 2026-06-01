#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Structured audit logging for API operations."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger

if TYPE_CHECKING:
    # Imported only for typing: at runtime the chain is injected via
    # ``configure_audit_chain`` to avoid an import cycle (audit_chain has no
    # dependency on this module, but the server lifespan wires them together).
    from provide.uterm.server.audit_chain import AuditChain

_audit_log = get_logger("provide.uterm.audit")

# Process-global tamper-evident WORM chain sink. None until the server lifespan
# enables it (audit.chain_enabled); reset to None on shutdown so a re-created
# app in the same process starts clean.
_chain: AuditChain | None = None


def configure_audit_chain(chain: AuditChain | None) -> None:
    """Install (or clear) the WORM audit chain that ``audit_event`` appends to.

    Called by the server lifespan: a real ``AuditChain`` on startup, ``None`` on
    shutdown. When unset, ``audit_event`` only emits the structured log.
    """
    global _chain
    _chain = chain


def audit_event(
    action: str,
    *,
    principal: str = "",
    session_id: str = "",
    source_ip: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    """Emit a structured audit log entry.

    Parameters
    ----------
    action:
        Dot-delimited action name, e.g. ``session.create``.
    principal:
        Authenticated subject identifier.
    session_id:
        Session or tunnel identifier (when applicable).
    source_ip:
        Client IP address from the request.
    detail:
        Arbitrary extra context for the event.
    """
    _audit_log.info(
        "audit action=%s principal=%s session_id=%s source_ip=%s",
        action,
        principal,
        session_id,
        source_ip,
        extra={
            "audit": True,
            "action": action,
            "principal": principal,
            "session_id": session_id,
            "source_ip": source_ip,
            "detail": detail or {},
            "ts": time.time(),
        },
    )
    # Best-effort tamper-evident WORM append. The chain append is fsync-durable
    # and synchronous; an append failure (disk full, perms, etc.) must NEVER
    # propagate into the request path, so swallow + warn. The structured log
    # above is the always-present record; the chain is the integrity layer.
    chain = _chain
    if chain is not None:
        try:
            chain.append(
                action,
                principal=principal,
                session_id=session_id,
                source_ip=source_ip,
                detail=detail or {},
            )
        except Exception:
            _audit_log.warning("audit_chain_append_failed action=%s", action)
