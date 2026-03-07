#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Authorization policy for hosted terminal server surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from provide.terminal.server.auth import Principal
    from provide.terminal.server.models import SessionDefinition

Role = str
Capability = str

ROLE_CAPABILITIES: dict[Role, frozenset[Capability]] = {
    "viewer": frozenset({"session.read", "session.recording.read"}),
    "operator": frozenset(
        {
            "session.read",
            "session.recording.read",
            "session.control.create",
            "session.control.connect",
            "session.control.mode",
            "session.control.clear",
            "session.control.update",
        }
    ),
    "admin": frozenset(
        {
            "session.read",
            "session.recording.read",
            "session.control.create",
            "session.control.connect",
            "session.control.mode",
            "session.control.clear",
            "session.control.update",
            "session.control.delete",
            "session.control.hijack",
        }
    ),
}


@dataclass(slots=True)
class AuthorizationService:
    """Role/capability and session visibility policy."""

    def capabilities_for(self, principal: Principal) -> frozenset[Capability]:
        caps: set[Capability] = set()
        for role in principal.roles:
            caps.update(ROLE_CAPABILITIES.get(role, frozenset()))
        return frozenset(caps)

    def has_role(self, principal: Principal, role: Role) -> bool:
        return role in principal.roles

    def has_capability(self, principal: Principal, capability: Capability) -> bool:
        return capability in self.capabilities_for(principal)

    def is_admin(self, principal: Principal) -> bool:
        return self.has_role(principal, "admin")

    def is_owner(self, principal: Principal, session: SessionDefinition) -> bool:
        return session.owner is not None and session.owner == principal.subject_id

    def can_read_session(self, principal: Principal, session: SessionDefinition) -> bool:
        if not self.has_capability(principal, "session.read"):
            return False
        if self.is_admin(principal) or self.is_owner(principal, session):
            return True
        if session.visibility == "public":
            return True
        if session.visibility == "operator":
            return self.has_role(principal, "operator")
        return False

    def can_read_recording(self, principal: Principal, session: SessionDefinition) -> bool:
        return self.can_read_session(principal, session) and self.has_capability(principal, "session.recording.read")

    def can_create_session(self, principal: Principal) -> bool:
        return self.has_capability(principal, "session.control.create")

    def can_mutate_session(self, principal: Principal, session: SessionDefinition, action: Capability) -> bool:
        if not self.has_capability(principal, action):
            return False
        if self.is_admin(principal):
            return True
        if session.owner is None:
            return False
        return self.is_owner(principal, session)

    def resolve_browser_role(self, principal: Principal, session: SessionDefinition) -> str:
        if not self.can_read_session(principal, session):
            return "viewer"
        if self.can_mutate_session(principal, session, "session.control.hijack"):
            return "admin"
        if self.has_role(principal, "operator") or self.is_owner(principal, session):
            return "operator"
        return "viewer"
