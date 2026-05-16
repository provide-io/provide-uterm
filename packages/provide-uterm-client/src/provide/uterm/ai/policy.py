#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Authorization policy table for MCP tool layer.

This module is the single source of truth that maps every MCP tool name to
the minimum role required to invoke it. Adding a new tool *must* register
its role here — :func:`required_role` raises if a tool is unknown, which
prevents an unguarded tool from slipping through the chokepoint.

Role ladder (matches ``provide.uterm.server.authorization``):

* ``viewer`` — read-only inspection.
* ``operator`` — session lifecycle, input mode, broadcast, annotation.
* ``admin`` — destructive / wide-blast-radius operations
  (worker disconnect, hijack lifecycle, fanout broadcast input,
  arbitrary connector spawn).
"""

from __future__ import annotations

from typing import Literal

Role = Literal["viewer", "operator", "admin"]

# Total ordering on roles (admin > operator > viewer).  Higher index ⇒ more
# privilege.  ``role_at_least(actual, minimum)`` uses this to decide whether
# *actual* satisfies the *minimum* requirement.
_ROLE_RANK: dict[str, int] = {"viewer": 0, "operator": 1, "admin": 2}


def role_rank(role: str) -> int:
    """Return the numeric rank for *role*; unknown roles rank below viewer."""
    return _ROLE_RANK.get(role, -1)


def role_at_least(actual: str, minimum: Role) -> bool:
    """Return ``True`` when *actual* is at least as privileged as *minimum*."""
    return role_rank(actual) >= role_rank(minimum)


# ---------------------------------------------------------------------------
# Tool → required role table.  Single source of truth.
# ---------------------------------------------------------------------------
#
# Hijack lifecycle (admin: takes exclusive control of a running worker).
# Session lifecycle / mode (operator: standard control-plane operations).
# Read-only inspection (viewer).
# Worker disconnect / arbitrary session_create / fanout_send (admin: broad
# blast radius — can spawn processes or disconnect production sessions).
TOOL_REQUIRED_ROLES: dict[str, Role] = {
    # Hijack lifecycle — exclusive worker takeover.
    "hijack_begin": "admin",
    "hijack_heartbeat": "admin",
    "hijack_read": "operator",
    "hijack_send": "admin",
    "hijack_step": "admin",
    "hijack_release": "admin",
    # Session read-only inspection.
    "session_list": "viewer",
    "session_status": "viewer",
    "session_read": "viewer",
    "server_health": "viewer",
    # Session lifecycle / mode changes.
    "session_connect": "operator",
    "session_disconnect": "operator",
    "session_set_mode": "operator",
    # Real-time event streams (read-only).
    "session_watch": "viewer",
    "session_subscribe": "viewer",
    # Annotations are operator-tier (write to recording timeline).
    "session_annotate": "operator",
    # Fanout group creation is operator (groups are configuration);
    # broadcasting input is admin (wide blast radius).
    "fanout_group_create": "operator",
    "fanout_send": "admin",
    # Arbitrary connector spawn / worker mode forcing / worker disconnect.
    "session_create": "admin",
    "worker_input_mode": "admin",
    "worker_disconnect": "admin",
}


def required_role(tool: str) -> Role:
    """Return the minimum role required to invoke *tool*.

    Raises
    ------
    KeyError
        If *tool* is not present in :data:`TOOL_REQUIRED_ROLES`.  Callers
        treat this as a programming error — every registered MCP tool must
        have an explicit policy entry.
    """
    try:
        return TOOL_REQUIRED_ROLES[tool]
    except KeyError as exc:  # pragma: no cover — defensive
        msg = f"No authorization policy registered for MCP tool {tool!r}"
        raise KeyError(msg) from exc


# ---------------------------------------------------------------------------
# session_create connector allowlist.
# ---------------------------------------------------------------------------
# Only well-known connectors are permitted to be spawned via the MCP layer.
# Adding new connectors requires an explicit entry here.
ALLOWED_CONNECTOR_TYPES: frozenset[str] = frozenset(
    {"shell", "telnet", "ssh", "ws", "websocket", "pty"},
)


def is_allowed_connector(connector_type: str) -> bool:
    """Return ``True`` when *connector_type* is on the spawn allowlist."""
    return connector_type in ALLOWED_CONNECTOR_TYPES
