#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Identity models and provider protocol."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.websockets import WebSocket

_TENANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def canonical_tenant_id(value: object | None) -> str | None:
    """Validate an untrusted tenant identifier without coercing its type."""
    if value is None:
        return None
    if not isinstance(value, str) or not _TENANT_ID.fullmatch(value):
        raise ValueError("tenant_id must be a safe identifier")
    return value


@dataclass(slots=True)
class Principal:
    """Resolved browser or API principal."""

    subject_id: str
    tenant_id: str | None = None
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    claims: dict[str, Any] = field(default_factory=dict)
    display_name: str | None = None
    # When set, the principal's ``admin`` role is confined to this single
    # session id: it grants admin capabilities on the named session only and
    # is NOT a global administrator. Used by tunnel share-operator principals
    # so a per-session grant cannot escalate into cross-session admin if the
    # principal is ever resolved independently of the request path.
    admin_session_scope: str | None = None

    @property
    def name(self) -> str:
        return self.display_name or self.subject_id

    @classmethod
    def anonymous(cls) -> Principal:
        """Create the intentionally tenantless unauthenticated identity."""
        return cls(subject_id="anonymous", tenant_id=None, roles=frozenset({"viewer"}), scopes=frozenset())

    @classmethod
    def system_worker(cls) -> Principal:
        """Create a tenantless infrastructure worker; never valid for tenant APIs."""
        return cls(subject_id="worker", tenant_id=None, roles=frozenset({"admin"}), scopes=frozenset({"*"}))

    @classmethod
    def session_share(
        cls,
        *,
        subject_id: str,
        roles: frozenset[str],
        scopes: frozenset[str],
        admin_session_scope: str | None = None,
    ) -> Principal:
        """Create a tenantless session-bound share identity."""
        return cls(
            subject_id=subject_id,
            tenant_id=None,
            roles=roles,
            scopes=scopes,
            admin_session_scope=admin_session_scope,
        )


@runtime_checkable
class IdentityProvider(Protocol):
    async def resolve_principal(self, connection: Request | WebSocket) -> Principal | None:
        """Extract credentials and return a resolved Principal."""
        ...
