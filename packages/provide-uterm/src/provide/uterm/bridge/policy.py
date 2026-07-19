#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Pure policy evaluation shared with Go/C# behavioral contract.

Mirrors packages/provide-uterm-go/policy Strict and the matrices in
spec/behavior.json so mutation and parametrized tests hit a real shipped path.
"""

from __future__ import annotations

from typing import Final

ROLE_RANK: Final[dict[str, int]] = {
    "viewer": 0,
    "operator": 1,
    "admin": 2,
}

ERR_INSUFFICIENT_ROLE: Final[str] = "forbidden: insufficient role"
ERR_NO_LEASE: Final[str] = "forbidden: no active lease"
ERR_SESSION_INACTIVE: Final[str] = "forbidden: session inactive"

_OP_MIN_ROLE: Final[dict[str, str]] = {
    "input_inject": "operator",
    "hijack_step": "operator",
    "hijack_release": "operator",
    "hijack_acquire": "operator",
}

_OP_NEEDS_LEASE: Final[frozenset[str]] = frozenset({"input_inject", "hijack_step"})
_OP_NEEDS_SESSION: Final[frozenset[str]] = frozenset({"hijack_step", "hijack_acquire"})


def role_rank(role: str) -> int:
    """Return rank for *role*, or -1 if unknown (always below viewer)."""
    if role not in ROLE_RANK:
        return -1
    return ROLE_RANK[role]


def can_perform(
    op: str,
    *,
    role: str,
    lease_owned: bool,
    session_active: bool = True,
) -> str | None:
    """Return None if *op* is allowed; otherwise a stable forbidden error string."""
    min_role = _OP_MIN_ROLE.get(op)
    if min_role is None:
        return f"forbidden: unknown operation {op}"
    if role_rank(role) < ROLE_RANK[min_role]:
        return ERR_INSUFFICIENT_ROLE
    if op in _OP_NEEDS_LEASE and not lease_owned:
        return ERR_NO_LEASE
    if op in _OP_NEEDS_SESSION and not session_active:
        return ERR_SESSION_INACTIVE
    return None


def can_inject(session_id: str, lease_id: str, principal_role: str) -> str | None:
    """Go Strict.CanInject equivalent (*session_id* reserved for audits).

    Does not pass ``session_active`` — ``input_inject`` does not require an
    active session, so the default is intentional and not a control point.
    """
    del session_id  # reserved for future audit correlation; must not gate inject
    return can_perform(
        "input_inject",
        role=principal_role,
        lease_owned=bool(lease_id),
    )


__all__ = [
    "ERR_INSUFFICIENT_ROLE",
    "ERR_NO_LEASE",
    "ERR_SESSION_INACTIVE",
    "ROLE_RANK",
    "can_inject",
    "can_perform",
    "role_rank",
]
