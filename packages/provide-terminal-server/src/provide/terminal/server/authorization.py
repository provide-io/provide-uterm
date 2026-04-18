#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Authorization policy for hosted terminal server surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from provide.terminal.server.auth import Principal
    from provide.terminal.server.models import SessionDefinition
    from provide.terminal.server.profiles import ConnectionProfile

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
        """Return the capability set granted to ``principal``.

        Roles define the maximum set.  When scopes are explicitly set on the
        principal (non-empty and not the ``"*"`` wildcard) they *narrow* the
        role-granted set — only capabilities named in scopes are granted.
        Empty scopes or ``{"*"}`` mean "unrestricted — use full role set".
        """
        role_caps: set[Capability] = set()
        for role in principal.roles:
            role_caps.update(ROLE_CAPABILITIES.get(role, frozenset()))
        if principal.scopes and "*" not in principal.scopes:
            return frozenset(cap for cap in role_caps if cap in principal.scopes)
        return frozenset(role_caps)

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
        # Tunnel share-token principals carry ``subject_id=share:{id}:{role}``
        # and are bound to the specific session the token was issued for.
        # Treat that as authoritative read access to *that* session only.
        if principal.subject_id.startswith(f"share:{session.session_id}:"):
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
        # Sessions without an explicit owner are system-managed and treated as
        # admin-only for mutation.  Non-admin principals can only mutate sessions
        # they own (i.e. sessions they created with their subject_id as owner).
        if session.owner is None:
            return False
        return self.is_owner(principal, session)

    def can_read_profile(self, principal: Principal, profile: ConnectionProfile) -> bool:
        return profile.owner == principal.subject_id or profile.visibility == "shared" or self.is_admin(principal)

    def can_mutate_profile(self, principal: Principal, profile: ConnectionProfile) -> bool:
        return profile.owner == principal.subject_id or self.is_admin(principal)

    def resolve_browser_role(self, principal: Principal, session: SessionDefinition) -> str:
        if not self.can_read_session(principal, session):
            return "viewer"
        if self.can_mutate_session(principal, session, "session.control.hijack"):
            return "admin"
        if self.has_role(principal, "operator") or self.is_owner(principal, session):
            return "operator"
        return "viewer"
