#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Structured audit logging for API operations."""

from __future__ import annotations

import time
from typing import Any

from provide.telemetry import get_logger

_audit_log = get_logger("provide.terminal.audit")


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
