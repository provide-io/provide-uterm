#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Pluggable authorization policy for hosted terminal server surfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx

if TYPE_CHECKING:
    from provide.uterm.server.auth import Principal
    from provide.uterm.server.models import SessionDefinition
    from provide.uterm.server.profiles import ConnectionProfile

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


@runtime_checkable
class AuthorizationProvider(Protocol):
    """Protocol for pluggable authorization decision engines."""

    async def capabilities_for(self, principal: Principal) -> frozenset[Capability]:
        """Return the capability set granted to ``principal``."""
        ...

    async def can_read_session(self, principal: Principal, session: SessionDefinition) -> bool:
        """Return True if ``principal`` can read the terminal data of ``session``."""
        ...

    async def can_read_recording(self, principal: Principal, session: SessionDefinition) -> bool:
        """Return True if ``principal`` can read the recording of ``session``."""
        ...

    async def can_create_session(self, principal: Principal) -> bool:
        """Return True if ``principal`` can create new terminal sessions."""
        ...

    async def can_mutate_session(self, principal: Principal, session: SessionDefinition, action: Capability) -> bool:
        """Return True if ``principal`` can perform ``action`` on ``session``."""
        ...

    async def can_read_profile(self, principal: Principal, profile: ConnectionProfile) -> bool:
        """Return True if ``principal`` can read ``profile``."""
        ...

    async def can_mutate_profile(self, principal: Principal, profile: ConnectionProfile) -> bool:
        """Return True if ``principal`` can modify or delete ``profile``."""
        ...

    async def resolve_browser_role(self, principal: Principal, session: SessionDefinition) -> str:
        """Resolve the browser-facing role string for ``principal`` on ``session``."""
        ...


class LocalAuthorizationProvider:
    """Standard AGPL RBAC implementation of AuthorizationProvider."""

    async def capabilities_for(self, principal: Principal) -> frozenset[Capability]:
        role_caps: set[Capability] = set()
        for role in principal.roles:
            role_caps.update(ROLE_CAPABILITIES.get(role, frozenset()))
        if principal.scopes and "*" not in principal.scopes:
            return frozenset(cap for cap in role_caps if cap in principal.scopes)
        return frozenset(role_caps)

    async def has_capability(self, principal: Principal, capability: Capability) -> bool:
        return capability in await self.capabilities_for(principal)

    async def is_admin(self, principal: Principal) -> bool:
        return "admin" in principal.roles

    async def is_owner(self, principal: Principal, session: SessionDefinition) -> bool:
        return session.owner is not None and session.owner == principal.subject_id

    async def can_read_session(self, principal: Principal, session: SessionDefinition) -> bool:
        if not await self.has_capability(principal, "session.read"):
            return False
        if await self.is_admin(principal) or await self.is_owner(principal, session):
            return True
        if principal.subject_id.startswith(f"share:{session.session_id}:"):
            return True
        if session.visibility == "public":
            return True
        if session.visibility == "operator":
            return "operator" in principal.roles
        return False

    async def can_read_recording(self, principal: Principal, session: SessionDefinition) -> bool:
        return await self.can_read_session(principal, session) and await self.has_capability(
            principal, "session.recording.read"
        )

    async def can_create_session(self, principal: Principal) -> bool:
        return await self.has_capability(principal, "session.control.create")

    async def can_mutate_session(self, principal: Principal, session: SessionDefinition, action: Capability) -> bool:
        if not await self.has_capability(principal, action):
            return False
        if await self.is_admin(principal):
            return True
        if session.owner is None:
            return False
        return await self.is_owner(principal, session)

    async def can_read_profile(self, principal: Principal, profile: ConnectionProfile) -> bool:
        return profile.owner == principal.subject_id or profile.visibility == "shared" or await self.is_admin(principal)

    async def can_mutate_profile(self, principal: Principal, profile: ConnectionProfile) -> bool:
        return profile.owner == principal.subject_id or await self.is_admin(principal)

    async def resolve_browser_role(self, principal: Principal, session: SessionDefinition) -> str:
        if not await self.can_read_session(principal, session):
            return "viewer"
        if await self.can_mutate_session(principal, session, "session.control.hijack"):
            return "admin"
        if "operator" in principal.roles or await self.is_owner(principal, session):
            return "operator"
        return "viewer"


class WebhookAuthorizationProvider:
    """Authorization provider that delegates decisions to an external webhook."""

    def __init__(self, url: str, secret: str | None = None, timeout_s: float = 2.0):
        self.url = url
        self.secret = secret
        self.timeout = timeout_s

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.secret:
            headers["X-Webhook-Secret"] = self.secret
        return headers

    async def _check(self, principal: Principal, action: str, **context: Any) -> bool:
        payload = {
            "principal": {
                "subject_id": principal.subject_id,
                "roles": list(principal.roles),
                "scopes": list(principal.scopes),
                "claims": principal.claims,
            },
            "action": action,
            "context": context,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.url, json=payload, headers=self._headers())
                if resp.status_code == 200:
                    return bool(resp.json().get("allow", False))
                return False
        except Exception:
            return False

    async def capabilities_for(self, principal: Principal) -> frozenset[Capability]:
        # Webhooks usually return specific booleans, but for full cap sets we might need a separate endpoint.
        # Fallback to empty if not implemented or error.
        payload = {"subject_id": principal.subject_id, "action": "capabilities"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.url, json=payload, headers=self._headers())
                if resp.status_code == 200:
                    return frozenset(resp.json().get("capabilities", []))
        except Exception:
            pass
        return frozenset()

    async def can_read_session(self, principal: Principal, session: SessionDefinition) -> bool:
        return await self._check(principal, "session.read", session_id=session.session_id)

    async def can_read_recording(self, principal: Principal, session: SessionDefinition) -> bool:
        return await self._check(principal, "session.recording.read", session_id=session.session_id)

    async def can_create_session(self, principal: Principal) -> bool:
        return await self._check(principal, "session.control.create")

    async def can_mutate_session(self, principal: Principal, session: SessionDefinition, action: Capability) -> bool:
        return await self._check(principal, action, session_id=session.session_id)

    async def can_read_profile(self, principal: Principal, profile: ConnectionProfile) -> bool:
        return await self._check(principal, "profile.read", profile_id=profile.profile_id)

    async def can_mutate_profile(self, principal: Principal, profile: ConnectionProfile) -> bool:
        return await self._check(principal, "profile.mutate", profile_id=profile.profile_id)

    async def resolve_browser_role(self, principal: Principal, session: SessionDefinition) -> str:
        # Complex resolution might be handled by the External Management Tier directly
        payload = {
            "principal": {
                "subject_id": principal.subject_id,
                "roles": list(principal.roles),
            },
            "session_id": session.session_id,
            "action": "resolve_role",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.url, json=payload, headers=self._headers())
                if resp.status_code == 200:
                    return str(resp.json().get("role", "viewer"))
        except Exception:
            pass
        return "viewer"


@dataclass(slots=True)
class AuthorizationService:
    """Pluggable gateway for authorization decisions."""

    _provider: AuthorizationProvider = field(default_factory=LocalAuthorizationProvider)

    async def capabilities_for(self, principal: Principal) -> frozenset[Capability]:
        return await self._provider.capabilities_for(principal)

    async def has_role(self, principal: Principal, role: Role) -> bool:
        return role in principal.roles

    async def has_capability(self, principal: Principal, capability: Capability) -> bool:
        provider = self._provider
        if hasattr(provider, "has_capability"):
            return await provider.has_capability(principal, capability)
        return capability in await provider.capabilities_for(principal)

    async def is_admin(self, principal: Principal) -> bool:
        provider = self._provider
        if hasattr(provider, "is_admin"):
            return await provider.is_admin(principal)
        return await LocalAuthorizationProvider().is_admin(principal)

    async def is_owner(self, principal: Principal, session: SessionDefinition) -> bool:
        provider = self._provider
        if hasattr(provider, "is_owner"):
            return await provider.is_owner(principal, session)
        return await LocalAuthorizationProvider().is_owner(principal, session)

    async def can_read_session(self, principal: Principal, session: SessionDefinition) -> bool:
        return await self._provider.can_read_session(principal, session)

    async def can_read_recording(self, principal: Principal, session: SessionDefinition) -> bool:
        return await self._provider.can_read_recording(principal, session)

    async def can_create_session(self, principal: Principal) -> bool:
        return await self._provider.can_create_session(principal)

    async def can_mutate_session(self, principal: Principal, session: SessionDefinition, action: Capability) -> bool:
        return await self._provider.can_mutate_session(principal, session, action)

    async def can_read_profile(self, principal: Principal, profile: ConnectionProfile) -> bool:
        return await self._provider.can_read_profile(principal, profile)

    async def can_mutate_profile(self, principal: Principal, profile: ConnectionProfile) -> bool:
        return await self._provider.can_mutate_profile(principal, profile)

    async def resolve_browser_role(self, principal: Principal, session: SessionDefinition) -> str:
        provider = self._provider
        if hasattr(provider, "resolve_browser_role"):
            return await provider.resolve_browser_role(principal, session)
        return await LocalAuthorizationProvider().resolve_browser_role(principal, session)
