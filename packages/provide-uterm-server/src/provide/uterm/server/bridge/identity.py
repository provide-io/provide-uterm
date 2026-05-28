#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Identity models and provider protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.websockets import WebSocket


@dataclass(slots=True)
class Principal:
    """Resolved browser or API principal."""

    subject_id: str
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    claims: dict[str, Any] = field(default_factory=dict)
    display_name: str | None = None

    @property
    def name(self) -> str:
        return self.display_name or self.subject_id


@runtime_checkable
class IdentityProvider(Protocol):
    async def resolve_principal(self, connection: Request | WebSocket) -> Principal | None:
        """Extract credentials and return a resolved Principal."""
        ...
