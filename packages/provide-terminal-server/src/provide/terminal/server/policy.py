#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Session-scoped policy decisions for the hosted server app."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from provide.terminal.server.authorization import AuthorizationService

if TYPE_CHECKING:
    from provide.terminal.server.auth import Principal
    from provide.terminal.server.models import AuthConfig, SessionDefinition


@dataclass(slots=True)
class SessionPolicyResolver:
    """Map principals to browser roles for a given session definition."""

    auth: AuthConfig
    authz: AuthorizationService = field(default_factory=AuthorizationService)

    async def role_for(self, principal: Principal, session: SessionDefinition) -> str:
        if self.auth.mode in {"none", "dev"} and not principal.roles:
            return "admin"
        return await self.authz.resolve_browser_role(principal, session)
