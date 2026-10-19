#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Pluggable authorization policy for hosted terminal server surfaces."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from provide.uterm.server import _http
from provide.uterm.server.egress import assert_webhook_target_allowed
from provide.uterm.server.webhook_signing import build_webhook_signature, verify_webhook_signature

if TYPE_CHECKING:
    from collections.abc import Callable

    from provide.uterm.server.auth import Principal
    from provide.uterm.server.models import SessionDefinition
    from provide.uterm.server.profiles import ConnectionProfile

Role = str
Capability = str

ROLE_CAPABILITIES: dict[Role, frozenset[Capability]] = {
    "viewer": frozenset({"session.read", "session.recording.read", "graphical.target.read"}),
    "operator": frozenset(
        {
            "session.read",
            "session.recording.read",
            "session.control.create",
            "session.control.connect",
            "session.control.mode",
            "session.control.clear",
            "session.control.update",
            "graphical.target.read",
            "graphical.target.manage",
            "graphical.session.attach",
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
            "graphical.target.read",
            "graphical.target.manage",
            "graphical.session.attach",
        }
    ),
}

# Revocation cutoffs held at once. One float per session whose viewers were
# revoked within the last SHARE_VIEWER_REVOCATION_TTL_S. When the table is full
# a revoke for a session not already in it is refused (the route answers 429)
# rather than evicting a cutoff that may still be protecting another session.
SHARE_VIEWER_REVOCATIONS_MAX = 10_000
# Cutoffs one revoking principal holds at once, so no single owner can fill the
# table above. Past it, that principal's own oldest cutoff makes room for its
# newest (the route audits each eviction). A global admin (the embedder's
# service token) is not capped.
SHARE_VIEWER_REVOCATIONS_PER_PRINCIPAL_MAX = 100
# How long a cutoff is kept at most. A cutoff only matters while a token issued
# before it can still be valid, so share tokens must live well under this
# (blackbetty mints 60 s ones): a share token that outlives its cutoff is
# admitted again. A capped revoker's cutoff can go sooner: it lasts
# min(24 h, until that principal's next SHARE_VIEWER_REVOCATIONS_PER_PRINCIPAL_MAX
# revokes of other sessions), so a non-admin that revokes more sessions than
# that within one share-token lifetime readmits its oldest session's tokens.
SHARE_VIEWER_REVOCATION_TTL_S = 86_400
# Subject cutoffs one session holds at once: one per share subject revoked on
# its own (a ``{"subject": ...}`` revoke) within the last
# SHARE_VIEWER_REVOCATION_TTL_S. Past it a new subject is refused (the route
# answers 429) rather than evicting one, which would readmit that watcher; a
# revoke of every viewer of the session frees them, since it subsumes them.
SHARE_VIEWER_SUBJECT_REVOCATIONS_PER_SESSION_MAX = 100
# Subject cutoffs held at once across every session, the counterpart of
# SHARE_VIEWER_REVOCATIONS_MAX: when it is full a new subject is refused (the
# route answers 429) rather than evicting a cutoff that may still protect
# another session's viewers.
SHARE_VIEWER_SUBJECT_REVOCATIONS_MAX = 10_000
# The longest share subject a revoke accepts, in UTF-8 bytes, so a held subject
# cutoff costs a bounded amount of memory. Subjects are minted by the embedder
# and are short (blackbetty's are ``share:{session}:blackbetty:{n}``).
SHARE_VIEWER_SUBJECT_MAX_BYTES = 512
# The largest revoke body read, in bytes: a SHARE_VIEWER_SUBJECT_MAX_BYTES
# subject in its ``{"subject": ...}`` envelope with room for whitespace and
# escapes. A longer body is refused before it is read in full.
SHARE_VIEWER_REVOKE_BODY_MAX_BYTES = 4096


class ShareViewerRevocationsFullError(Exception):
    """A cutoff table is full of live cutoffs; a new session or subject cannot be added."""


class ShareViewerSubjectRevocationsFullError(Exception):
    """The session holds its cap of subject cutoffs; a new subject cannot be added."""


def is_share_viewer(principal: Principal, session_id: str) -> bool:
    """True when ``principal`` is a read-only share grant for ``session_id``.

    That is a ``share:{session_id}:...`` subject holding nothing beyond the
    ``viewer`` role and no session-scoped admin grant -- the token an embedder
    mints so someone can watch one session. A tunnel share *operator* uses the
    same subject prefix but holds a session-scoped ``admin`` grant, so it is
    not one, and neither is an owner, an operator or a global admin.

    Nor is a tunnel share viewer (``share:{id}:viewer`` from a tunnel share
    cookie): tunnel shares have their own revocation, token rotation, and a
    viewer cutoff would also refuse the links a rotation hands out.
    """
    return (
        principal.subject_id.startswith(f"share:{session_id}:")
        and not principal.tunnel_share
        and principal.roles <= frozenset({"viewer"})
        and principal.admin_session_scope is None
    )


def _issued_at(principal: Principal) -> float | None:
    """The token's numeric ``iat`` claim, or ``None`` when it has none."""
    raw = principal.claims.get("iat")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return float(raw)


@runtime_checkable
class AuthorizationProvider(Protocol):
    """Protocol for pluggable authorization decision engines."""

    async def capabilities_for(self, principal: Principal) -> frozenset[Capability]:
        """Return the capability set granted to ``principal``."""
        ...

    async def has_capability(self, principal: Principal, capability: Capability) -> bool:
        """Return True if ``principal`` has ``capability``."""
        ...

    async def is_admin(self, principal: Principal) -> bool:
        """Return True if ``principal`` has administrator privileges."""
        ...

    async def is_owner(self, principal: Principal, session: SessionDefinition) -> bool:
        """Return True if ``principal`` owns ``session``."""
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
        # A session-scoped admin grant (tunnel share-operator) is NOT a global
        # administrator: it confers admin rights on a single session only. Such
        # principals must fail the global admin check so the grant cannot leak
        # to other sessions; per-session admin is resolved via
        # ``_is_admin_for_session``.
        return "admin" in principal.roles and principal.admin_session_scope is None

    @staticmethod
    def _is_admin_for_session(principal: Principal, session: SessionDefinition) -> bool:
        """True if ``principal`` has admin rights over this specific session.

        Covers global admins (``admin_session_scope is None``) and
        session-scoped admins whose scope matches ``session.session_id``.
        """
        if "admin" not in principal.roles:
            return False
        scope = principal.admin_session_scope
        return scope is None or scope == session.session_id

    async def is_owner(self, principal: Principal, session: SessionDefinition) -> bool:
        return session.owner is not None and session.owner == principal.subject_id

    async def can_read_session(self, principal: Principal, session: SessionDefinition) -> bool:
        if not await self.has_capability(principal, "session.read"):
            return False
        if self._is_admin_for_session(principal, session) or await self.is_owner(principal, session):
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
        if self._is_admin_for_session(principal, session):
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

    def __init__(
        self,
        url: str,
        secret: str | None = None,
        timeout_s: float = 2.0,
        *,
        require_signed_response: bool = True,
    ):
        self.url = url
        self.secret = secret
        self.timeout = timeout_s
        # Parity with webhook IdP: when a shared secret is configured, require a
        # signed response by default so a MITM cannot mint {"allow": true}.
        # When no secret is configured, signed-response verification is skipped
        # (callers should set a secret in production).
        self.require_signed_response = require_signed_response and bool((secret or "").strip())
        # Reuse one client across calls so HTTP keep-alive / connection pooling
        # survives between authorization checks. Constructing it here opens no
        # sockets — the async client connects lazily on the first request.
        self._client = _http.async_client(timeout=timeout_s)

    async def aclose(self) -> None:
        """Release the shared client's connection pool (lifecycle cleanup)."""
        await self._client.aclose()

    def _signed_headers(self, body: bytes) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.secret:
            ts = str(time.time())
            headers["X-Uterm-Timestamp"] = ts
            headers["X-Uterm-Signature"] = build_webhook_signature(self.secret, body, ts)
        return headers

    def _response_signature_ok(self, resp: _http.Response) -> bool:
        if not self.require_signed_response:
            return True
        return verify_webhook_signature(
            self.secret,
            resp.content,
            resp.headers.get("X-Uterm-Signature") or resp.headers.get("x-uterm-signature"),
            resp.headers.get("X-Uterm-Timestamp") or resp.headers.get("x-uterm-timestamp"),
        )

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
        body = json.dumps(payload, separators=(",", ":")).encode()
        try:
            await assert_webhook_target_allowed(self.url)
            resp = await self._client.post(self.url, content=body, headers=self._signed_headers(body))
            if resp.status_code == 200:
                if not self._response_signature_ok(resp):
                    return False
                return bool(resp.json().get("allow", False))
            return False
        except Exception:
            return False

    async def capabilities_for(self, principal: Principal) -> frozenset[Capability]:
        # Webhooks usually return specific booleans, but for full cap sets we might need a separate endpoint.
        # Fallback to empty if not implemented or error.
        payload = {"subject_id": principal.subject_id, "action": "capabilities"}
        body = json.dumps(payload, separators=(",", ":")).encode()
        try:
            await assert_webhook_target_allowed(self.url)
            resp = await self._client.post(self.url, content=body, headers=self._signed_headers(body))
            if resp.status_code == 200 and self._response_signature_ok(resp):
                return frozenset(resp.json().get("capabilities", []))
        except Exception:
            # Fail closed: an egress block, transport error or bad body grants
            # no capabilities (the empty set below), like ``_check`` returning False.
            pass
        return frozenset()

    async def has_capability(self, principal: Principal, capability: Capability) -> bool:
        return await self._check(principal, capability)

    async def is_admin(self, principal: Principal) -> bool:
        """Finding #8: delegate admin-check to the webhook.

        Without this method, ``AuthorizationService.is_admin`` falls back to
        ``LocalAuthorizationProvider().is_admin`` and consults
        ``principal.roles`` directly — bypassing the webhook policy.  A
        webhook-driven deployment that revoked a user's admin role at the
        policy engine would still have callers pass admin checks because the
        ``admin`` role string is still in their JWT.  Delegating to the
        webhook keeps a single source of truth.
        """
        return await self._check(principal, "admin")

    async def is_owner(self, principal: Principal, session: SessionDefinition) -> bool:
        return await self._check(principal, "session.owner", session_id=session.session_id)

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
        body = json.dumps(payload, separators=(",", ":")).encode()
        try:
            await assert_webhook_target_allowed(self.url)
            resp = await self._client.post(self.url, content=body, headers=self._signed_headers(body))
            if resp.status_code == 200:
                # Filter the webhook-returned role to the canonical allow-list
                # at the boundary (case-folded), mirroring the IDP role path —
                # a compromised/misconfigured policy engine must not be able to
                # mint a privileged or bogus role string. ``_filter_known_roles``
                # yields a non-empty frozenset (falling back to viewer), so
                # ``next(iter(...))`` is always safe.
                from provide.uterm.server.auth_roles import _filter_known_roles

                raw_role = resp.json().get("role", "viewer")
                return next(iter(_filter_known_roles([raw_role])))
        except Exception:
            # Fail closed: any egress/transport/body error resolves to the
            # least-privileged "viewer" role below.
            pass
        return "viewer"


@dataclass(slots=True)
class AuthorizationService:
    """Pluggable gateway for authorization decisions."""

    _provider: AuthorizationProvider = field(default_factory=LocalAuthorizationProvider)
    # session_id -> wall-clock cutoff. A share viewer of that session whose token
    # was issued at or before the cutoff is refused (see ``share_viewer_revoked``).
    # Process-local, like the tunnel token store.
    _share_viewer_revocations: dict[str, float] = field(default_factory=dict)
    # session_id -> the capped principal it counts against (the one that first
    # revoked it, while it is held), and that principal's sessions oldest first
    # (dicts as ordered sets).
    _share_viewer_revokers: dict[str, str] = field(default_factory=dict)
    _share_viewer_revoked_by: dict[str, dict[str, None]] = field(default_factory=dict)
    # (session_id, subject) -> wall-clock cutoff for one share subject of the
    # session, oldest first, and each session's subjects (dicts as ordered
    # sets), so neither a session's count nor its deletion needs a scan.
    _share_viewer_subject_revocations: dict[tuple[str, str], float] = field(default_factory=dict)
    _share_viewer_revoked_subjects: dict[str, dict[str, None]] = field(default_factory=dict)

    def revoke_share_viewers(
        self,
        session_id: str,
        *,
        now: float | None = None,
        revoker: str | None = None,
        on_evict: Callable[[str], None] | None = None,
    ) -> float:
        """Refuse every share-viewer token for ``session_id`` issued up to now.

        Returns the cutoff; raises ``ShareViewerRevocationsFullError`` when the
        table is full and this session holds no cutoff yet. Wall clock, because that is the clock a token's
        ``iat`` is written in. A token issued in the same second as the cutoff
        is refused too: ``iat`` has one-second resolution, so it cannot say
        which side of the cutoff it fell on.

        ``revoker`` is the subject a new cutoff counts against, at most
        ``SHARE_VIEWER_REVOCATIONS_PER_PRINCIPAL_MAX`` of them, its oldest
        evicted first; ``None`` (a global admin) is not capped. ``on_evict`` is
        called with the session whose cutoff that eviction dropped. A re-revoke
        moves the cutoff forward but leaves it counted against whoever held it,
        so no principal can pull another's cutoff into its own quota and then
        flush it out.
        """
        cutoff = time.time() if now is None else now
        revocations = self._share_viewer_revocations
        # Insertion order is revocation order, so the stale ones are in front.
        while revocations and revocations[oldest := next(iter(revocations))] < cutoff - SHARE_VIEWER_REVOCATION_TTL_S:
            self._drop_share_viewer_revocation(oldest)
        seen = session_id in revocations
        owner = self._share_viewer_revokers.get(session_id) if seen else revoker
        self._drop_share_viewer_revocation(session_id)
        evicted = None
        if not seen and revoker is not None:
            mine = self._share_viewer_revoked_by.get(revoker, {})
            if len(mine) >= SHARE_VIEWER_REVOCATIONS_PER_PRINCIPAL_MAX:
                evicted = next(iter(mine))
                self._drop_share_viewer_revocation(evicted)
        # Nothing was dropped above unless this session or the revoker's own
        # oldest made room, so a refusal leaves the table as it was.
        if not seen and len(revocations) >= SHARE_VIEWER_REVOCATIONS_MAX:
            raise ShareViewerRevocationsFullError
        revocations[session_id] = cutoff
        if owner is not None:
            self._share_viewer_revokers[session_id] = owner
            self._share_viewer_revoked_by.setdefault(owner, {})[session_id] = None
        # The session's subject cutoffs at or before this one refuse nothing it
        # does not (the later cutoff decides), so they go and free their slots.
        for subject in list(self._share_viewer_revoked_subjects.get(session_id, {})):
            if self._share_viewer_subject_revocations[(session_id, subject)] <= cutoff:
                self._drop_share_viewer_subject(session_id, subject)
        if evicted is not None and on_evict is not None:
            on_evict(evicted)
        return cutoff

    def _drop_share_viewer_revocation(self, session_id: str) -> None:
        self._share_viewer_revocations.pop(session_id, None)
        revoker = self._share_viewer_revokers.pop(session_id, None)
        if revoker is not None:
            mine = self._share_viewer_revoked_by[revoker]
            del mine[session_id]
            if not mine:
                del self._share_viewer_revoked_by[revoker]

    def revoke_share_viewer_subject(self, session_id: str, subject: str, *, now: float | None = None) -> float:
        """Refuse ``subject``'s share-viewer tokens for ``session_id`` issued up to now.

        The cutoff ``revoke_share_viewers`` records, for one share subject: the
        same wall clock, and a token issued in the same second refused too. The
        session's other share viewers are untouched, and a token minted for
        ``subject`` after the cutoff is admitted, so that watcher can be let
        back in. Returns the cutoff; raises
        ``ShareViewerSubjectRevocationsFullError`` when the session already
        holds ``SHARE_VIEWER_SUBJECT_REVOCATIONS_PER_SESSION_MAX`` subject
        cutoffs and none of them is ``subject``'s, and
        ``ShareViewerRevocationsFullError`` when every session together holds
        ``SHARE_VIEWER_SUBJECT_REVOCATIONS_MAX`` and this is a new one. The
        caller checks that ``subject`` is one of this session's share subjects.
        """
        cutoff = time.time() if now is None else now
        cutoffs = self._share_viewer_subject_revocations
        # Insertion order is revocation order, so the stale ones are in front.
        while cutoffs and cutoffs[oldest := next(iter(cutoffs))] < cutoff - SHARE_VIEWER_REVOCATION_TTL_S:
            self._drop_share_viewer_subject(*oldest)
        held = self._share_viewer_revoked_subjects.get(session_id, {})
        if subject not in held and len(held) >= SHARE_VIEWER_SUBJECT_REVOCATIONS_PER_SESSION_MAX:
            raise ShareViewerSubjectRevocationsFullError
        if subject not in held and len(cutoffs) >= SHARE_VIEWER_SUBJECT_REVOCATIONS_MAX:
            raise ShareViewerRevocationsFullError
        # Dropped and re-added, so a re-revoke moves to the back of the order
        # and ages out from its newest cutoff.
        self._drop_share_viewer_subject(session_id, subject)
        cutoffs[(session_id, subject)] = cutoff
        self._share_viewer_revoked_subjects.setdefault(session_id, {})[subject] = None
        return cutoff

    def _drop_share_viewer_subject(self, session_id: str, subject: str) -> None:
        if self._share_viewer_subject_revocations.pop((session_id, subject), None) is None:
            return
        held = self._share_viewer_revoked_subjects[session_id]
        del held[subject]
        if not held:
            del self._share_viewer_revoked_subjects[session_id]

    def share_viewer_revoked_sessions(self) -> list[str]:
        """The sessions holding a cutoff of either kind, each once."""
        return list(dict.fromkeys([*self._share_viewer_revocations, *self._share_viewer_revoked_subjects]))

    def forget_share_viewers(self, session_id: str) -> None:
        """Drop the session's cutoffs (it was deleted, so nothing is left to refuse)."""
        self._drop_share_viewer_revocation(session_id)
        for subject in list(self._share_viewer_revoked_subjects.get(session_id, {})):
            self._drop_share_viewer_subject(session_id, subject)

    def share_viewer_revocation(self, session_id: str) -> float | None:
        """The cutoff recorded for ``session_id``, if its viewers were revoked."""
        return self._share_viewer_revocations.get(session_id)

    def share_viewer_subject_revocation(self, session_id: str, subject: str) -> float | None:
        """The cutoff recorded for one share subject of ``session_id``, if it was revoked."""
        return self._share_viewer_subject_revocations.get((session_id, subject))

    def share_viewer_revoked(self, principal: Principal, session_id: str) -> bool:
        """True when ``principal`` is a share viewer whose token predates a cutoff.

        Both the session's cutoff and its own subject's count, so the later of
        the two decides. A token without a numeric ``iat`` cannot show it
        postdates the cutoff, so it is refused.
        """
        held = (
            self._share_viewer_revocations.get(session_id),
            self._share_viewer_subject_revocations.get((session_id, principal.subject_id)),
        )
        cutoff = max((one for one in held if one is not None), default=None)
        if cutoff is None or not is_share_viewer(principal, session_id):
            return False
        issued = _issued_at(principal)
        return issued is None or issued <= cutoff

    async def capabilities_for(self, principal: Principal) -> frozenset[Capability]:
        provider: Any = self._provider
        method = getattr(provider, "capabilities_for", None)
        if callable(method):
            return cast("frozenset[Capability]", await method(principal))
        return await LocalAuthorizationProvider().capabilities_for(principal)

    async def has_role(self, principal: Principal, role: Role) -> bool:
        return role in principal.roles

    async def has_capability(self, principal: Principal, capability: Capability) -> bool:
        provider: Any = self._provider
        method = getattr(provider, "has_capability", None)
        if callable(method):
            return bool(await method(principal, capability))
        return capability in await self.capabilities_for(principal)

    async def is_admin(self, principal: Principal) -> bool:
        provider: Any = self._provider
        method = getattr(provider, "is_admin", None)
        if callable(method):
            return bool(await method(principal))
        return await LocalAuthorizationProvider().is_admin(principal)

    async def is_owner(self, principal: Principal, session: SessionDefinition) -> bool:
        provider: Any = self._provider
        method = getattr(provider, "is_owner", None)
        if callable(method):
            return bool(await method(principal, session))
        return await LocalAuthorizationProvider().is_owner(principal, session)

    async def can_read_session(self, principal: Principal, session: SessionDefinition) -> bool:
        # Checked here, ahead of the provider, so no provider -- local RBAC, a
        # webhook, a visibility of "public" -- can readmit a revoked viewer.
        if self.share_viewer_revoked(principal, session.session_id):
            return False
        provider: Any = self._provider
        method = getattr(provider, "can_read_session", None)
        if callable(method):
            return bool(await method(principal, session))
        return await LocalAuthorizationProvider().can_read_session(principal, session)

    async def can_read_recording(self, principal: Principal, session: SessionDefinition) -> bool:
        if self.share_viewer_revoked(principal, session.session_id):
            return False
        provider: Any = self._provider
        method = getattr(provider, "can_read_recording", None)
        if callable(method):
            return bool(await method(principal, session))
        return await LocalAuthorizationProvider().can_read_recording(principal, session)

    async def can_create_session(self, principal: Principal) -> bool:
        provider: Any = self._provider
        method = getattr(provider, "can_create_session", None)
        if callable(method):
            return bool(await method(principal))
        return await LocalAuthorizationProvider().can_create_session(principal)

    async def can_mutate_session(self, principal: Principal, session: SessionDefinition, action: Capability) -> bool:
        provider: Any = self._provider
        method = getattr(provider, "can_mutate_session", None)
        if callable(method):
            return bool(await method(principal, session, action))
        return await LocalAuthorizationProvider().can_mutate_session(principal, session, action)

    async def can_read_profile(self, principal: Principal, profile: ConnectionProfile) -> bool:
        provider: Any = self._provider
        method = getattr(provider, "can_read_profile", None)
        if callable(method):
            return bool(await method(principal, profile))
        return await LocalAuthorizationProvider().can_read_profile(principal, profile)

    async def can_mutate_profile(self, principal: Principal, profile: ConnectionProfile) -> bool:
        provider: Any = self._provider
        method = getattr(provider, "can_mutate_profile", None)
        if callable(method):
            return bool(await method(principal, profile))
        return await LocalAuthorizationProvider().can_mutate_profile(principal, profile)

    async def resolve_browser_role(self, principal: Principal, session: SessionDefinition) -> str:
        provider = self._provider
        if hasattr(provider, "resolve_browser_role"):
            return await provider.resolve_browser_role(principal, session)
        return await LocalAuthorizationProvider().resolve_browser_role(principal, session)

    async def aclose(self) -> None:
        """Release any provider-held resources (e.g. a pooled webhook client).

        Forwards to the provider's ``aclose`` when it defines one so the app
        lifespan can close the webhook connection pool on shutdown; a no-op for
        providers (like the local RBAC default) that hold no such resources.
        """
        provider: Any = self._provider
        method = getattr(provider, "aclose", None)
        if callable(method):
            await method()
