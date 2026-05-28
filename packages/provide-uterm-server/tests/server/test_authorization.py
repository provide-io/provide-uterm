#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for server authorization.py — AuthorizationService capability/role checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from provide.uterm.server.auth import Principal
from provide.uterm.server.authorization import AuthorizationService
from provide.uterm.server.models import SessionDefinition

if TYPE_CHECKING:
    from provide.uterm.server.profiles import ConnectionProfile


@pytest.fixture()
def authz() -> AuthorizationService:
    return AuthorizationService()


def _principal(subject_id: str = "user", roles: list[str] | None = None) -> Principal:
    return Principal(subject_id=subject_id, roles=frozenset(roles or ["operator"]))


def _session(
    session_id: str = "sess1",
    owner: str | None = None,
    visibility: str = "public",
) -> SessionDefinition:
    return SessionDefinition(
        session_id=session_id,
        display_name="Test",
        connector_type="shell",
        owner=owner,
        visibility=visibility,  # type: ignore[arg-type]
    )


class TestCapabilitiesFor:
    async def test_viewer_has_read_caps(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["viewer"])
        caps = await authz.capabilities_for(p)
        assert "session.read" in caps
        assert "session.control.create" not in caps

    async def test_operator_has_create_and_connect(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["operator"])
        caps = await authz.capabilities_for(p)
        assert "session.control.create" in caps
        assert "session.control.delete" not in caps

    async def test_admin_has_all_caps(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["admin"])
        caps = await authz.capabilities_for(p)
        assert "session.control.delete" in caps
        assert "session.control.hijack" in caps

    async def test_unknown_role_contributes_nothing(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["unknown_role"])
        caps = await authz.capabilities_for(p)
        assert len(caps) == 0

    async def test_wildcard_scope_grants_full_role_caps(self, authz: AuthorizationService) -> None:
        """``scopes={'*'}`` is treated as unrestricted — full role caps are kept."""
        p = Principal(subject_id="u", roles=frozenset({"admin"}), scopes=frozenset({"*"}))
        caps = await authz.capabilities_for(p)
        assert "session.control.delete" in caps

    async def test_empty_scopes_grants_full_role_caps(self, authz: AuthorizationService) -> None:
        """Empty ``scopes`` is treated as unrestricted (no constraint)."""
        p = Principal(subject_id="u", roles=frozenset({"admin"}), scopes=frozenset())
        caps = await authz.capabilities_for(p)
        assert "session.control.delete" in caps

    async def test_scopes_narrow_role_caps(self, authz: AuthorizationService) -> None:
        """An admin with scope ``{'session.read'}`` keeps ONLY session.read."""
        p = Principal(subject_id="u", roles=frozenset({"admin"}), scopes=frozenset({"session.read"}))
        caps = await authz.capabilities_for(p)
        assert caps == frozenset({"session.read"})
        assert "session.control.delete" not in caps

    async def test_scopes_cannot_grant_caps_beyond_role(self, authz: AuthorizationService) -> None:
        """Scope ``{'session.control.delete'}`` on a viewer doesn't upgrade to delete."""
        p = Principal(
            subject_id="u",
            roles=frozenset({"viewer"}),
            scopes=frozenset({"session.control.delete"}),
        )
        caps = await authz.capabilities_for(p)
        # session.control.delete is not in viewer role → scope can't grant it
        assert "session.control.delete" not in caps
        # And session.read is not in the narrowed set either (scope excludes it)
        assert "session.read" not in caps


class TestCanReadSession:
    async def test_viewer_can_read_public(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["viewer"])
        s = _session(visibility="public")
        assert await authz.can_read_session(p, s) is True

    async def test_no_read_cap_returns_false(self, authz: AuthorizationService) -> None:
        # An unknown role has no caps at all — can't read anything
        p = _principal(roles=["unknown_role"])
        s = _session(visibility="public")
        assert await authz.can_read_session(p, s) is False  # line 73

    async def test_admin_can_read_private(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["admin"])
        s = _session(visibility="private", owner="someone_else")
        assert await authz.can_read_session(p, s) is True

    async def test_owner_can_read_private(self, authz: AuthorizationService) -> None:
        p = _principal(subject_id="alice", roles=["operator"])
        s = _session(visibility="private", owner="alice")
        assert await authz.can_read_session(p, s) is True

    async def test_operator_can_read_operator_visibility(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["operator"])
        s = _session(visibility="operator")
        assert await authz.can_read_session(p, s) is True  # line 79: has_role("operator") → True

    async def test_viewer_cannot_read_operator_visibility(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["viewer"])
        s = _session(visibility="operator")
        assert await authz.can_read_session(p, s) is False  # line 79: has_role("operator") → False

    async def test_viewer_cannot_read_private(self, authz: AuthorizationService) -> None:
        p = _principal(subject_id="bob", roles=["viewer"])
        s = _session(visibility="private", owner="alice")
        assert await authz.can_read_session(p, s) is False

    async def test_share_token_principal_can_read_its_tunnel(self, authz: AuthorizationService) -> None:
        """A share-token principal bound to tunnel-abc sees that specific session."""
        p = _principal(subject_id="share:tunnel-abc:viewer", roles=["viewer"])
        s = _session(session_id="tunnel-abc", visibility="private", owner="alice")
        assert await authz.can_read_session(p, s) is True

    async def test_share_token_principal_cannot_read_other_tunnel(self, authz: AuthorizationService) -> None:
        """A share-token principal for tunnel-abc must NOT read tunnel-xyz."""
        p = _principal(subject_id="share:tunnel-abc:viewer", roles=["viewer"])
        s = _session(session_id="tunnel-xyz", visibility="private", owner="alice")
        assert await authz.can_read_session(p, s) is False


class TestCanMutateSession:
    async def test_admin_can_mutate_any_session(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["admin"])
        s = _session(owner=None)
        assert await authz.can_mutate_session(p, s, "session.control.update") is True

    async def test_system_session_no_owner_blocks_operator(self, authz: AuthorizationService) -> None:
        # session.owner is None — system-managed; operators cannot mutate
        p = _principal(roles=["operator"])
        s = _session(owner=None)
        assert await authz.can_mutate_session(p, s, "session.control.update") is False  # line 97

    async def test_operator_can_mutate_owned_session(self, authz: AuthorizationService) -> None:
        p = _principal(subject_id="alice", roles=["operator"])
        s = _session(owner="alice")
        assert await authz.can_mutate_session(p, s, "session.control.update") is True  # line 102

    async def test_operator_cannot_mutate_others_session(self, authz: AuthorizationService) -> None:
        p = _principal(subject_id="bob", roles=["operator"])
        s = _session(owner="alice")
        assert await authz.can_mutate_session(p, s, "session.control.update") is False

    async def test_missing_capability_blocks_admin(self, authz: AuthorizationService) -> None:
        # Even admin needs the capability in the map — delete is admin-only
        p = _principal(roles=["operator"])
        s = _session(owner="operator_user")
        assert await authz.can_mutate_session(p, s, "session.control.delete") is False


class TestSessionScopedShareOperator:
    """SRV-share: a share-operator principal's admin grant is scoped to one session.

    The tunnel share-operator carries admin capabilities so it can drive its own
    session, but that admin grant must NOT leak to other sessions. A principal
    scoped to session A must be denied admin operations on session B.
    """

    def _scoped(self, session_id: str) -> Principal:
        return Principal(
            subject_id=f"share:{session_id}:operator",
            roles=frozenset({"admin"}),
            scopes=frozenset({"*"}),
            admin_session_scope=session_id,
        )

    async def test_scoped_operator_is_not_a_global_admin(self, authz: AuthorizationService) -> None:
        p = self._scoped("A")
        assert await authz.is_admin(p) is False

    async def test_scoped_operator_can_mutate_its_own_session(self, authz: AuthorizationService) -> None:
        p = self._scoped("A")
        s = _session(session_id="A", owner=None)
        assert await authz.can_mutate_session(p, s, "session.control.hijack") is True

    async def test_scoped_operator_cannot_mutate_other_session(self, authz: AuthorizationService) -> None:
        p = self._scoped("A")
        s = _session(session_id="B", owner=None)
        assert await authz.can_mutate_session(p, s, "session.control.hijack") is False

    async def test_scoped_operator_can_read_its_own_session(self, authz: AuthorizationService) -> None:
        p = self._scoped("A")
        s = _session(session_id="A", owner=None, visibility="private")
        assert await authz.can_read_session(p, s) is True

    async def test_scoped_operator_cannot_read_other_private_session(self, authz: AuthorizationService) -> None:
        p = self._scoped("A")
        s = _session(session_id="B", owner=None, visibility="private")
        assert await authz.can_read_session(p, s) is False


class TestResolveBrowserRole:
    async def test_admin_principal_gets_admin_role(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["admin"])
        s = _session(visibility="public")
        assert await authz.resolve_browser_role(p, s) == "admin"

    async def test_operator_gets_operator_role(self, authz: AuthorizationService) -> None:
        p = _principal(subject_id="op", roles=["operator"])
        s = _session(visibility="public", owner=None)  # operator but not owner
        assert await authz.resolve_browser_role(p, s) == "operator"  # line 106

    async def test_owner_gets_operator_role(self, authz: AuthorizationService) -> None:
        p = _principal(subject_id="alice", roles=["operator"])
        s = _session(visibility="public", owner="alice")
        # is_owner → True, but not admin → resolve to "operator"
        role = await authz.resolve_browser_role(p, s)
        assert role in {"operator", "admin"}  # owner with operator role

    async def test_viewer_gets_viewer_role(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["viewer"])
        s = _session(visibility="public")
        assert await authz.resolve_browser_role(p, s) == "viewer"

    async def test_no_read_access_gets_viewer(self, authz: AuthorizationService) -> None:
        # Unknown role: no caps → can_read_session = False → viewer
        p = _principal(roles=["unknown"])
        s = _session(visibility="public")
        assert await authz.resolve_browser_role(p, s) == "viewer"


# ── Profile authorization ─────────────────────────────────────────────────


def _make_test_profile(owner: str, visibility: str = "private") -> ConnectionProfile:
    """Return a minimal ConnectionProfile for auth tests."""
    import time

    from provide.uterm.server.profiles import ConnectionProfile

    now = time.time()
    return ConnectionProfile(
        profile_id="profile-test",
        owner=owner,
        name="Test",
        connector_type="ssh",
        visibility=visibility,  # type: ignore[arg-type]
        created_at=now,
        updated_at=now,
    )


async def test_can_read_own_private_profile() -> None:
    authz = AuthorizationService()
    principal = _principal("alice", roles=["operator"])
    profile = _make_test_profile(owner="alice", visibility="private")
    assert await authz.can_read_profile(principal, profile) is True


async def test_cannot_read_other_private_profile() -> None:
    authz = AuthorizationService()
    principal = _principal("alice", roles=["operator"])
    profile = _make_test_profile(owner="bob", visibility="private")
    assert await authz.can_read_profile(principal, profile) is False


async def test_can_read_shared_profile_as_non_owner() -> None:
    authz = AuthorizationService()
    principal = _principal("alice", roles=["operator"])
    profile = _make_test_profile(owner="bob", visibility="shared")
    assert await authz.can_read_profile(principal, profile) is True


async def test_admin_can_read_any_profile() -> None:
    authz = AuthorizationService()
    principal = _principal("admin", roles=["admin"])
    profile = _make_test_profile(owner="bob", visibility="private")
    assert await authz.can_read_profile(principal, profile) is True


async def test_can_mutate_own_profile() -> None:
    authz = AuthorizationService()
    principal = _principal("alice", roles=["operator"])
    profile = _make_test_profile(owner="alice")
    assert await authz.can_mutate_profile(principal, profile) is True


async def test_cannot_mutate_other_profile() -> None:
    authz = AuthorizationService()
    principal = _principal("alice", roles=["operator"])
    profile = _make_test_profile(owner="bob")
    assert await authz.can_mutate_profile(principal, profile) is False


async def test_admin_can_mutate_any_profile() -> None:
    authz = AuthorizationService()
    principal = _principal("admin", roles=["admin"])
    profile = _make_test_profile(owner="bob")
    assert await authz.can_mutate_profile(principal, profile) is True


async def test_authorization_service_uses_local_fallbacks_for_partial_provider() -> None:
    class EmptyProvider:
        pass

    authz = AuthorizationService(EmptyProvider())  # type: ignore[arg-type]

    admin = _principal("admin", roles=["admin"])
    operator = _principal("op", roles=["operator"])
    viewer = _principal("viewer", roles=["viewer"])
    public_session = _session(visibility="public")
    profile = _make_test_profile(owner="op", visibility="private")

    assert "session.control.delete" in await authz.capabilities_for(admin)
    assert await authz.can_read_session(viewer, public_session) is True
    assert await authz.can_read_recording(viewer, public_session) is True
    assert await authz.can_create_session(operator) is True
    assert await authz.can_mutate_session(admin, public_session, "session.control.delete") is True
    assert await authz.can_read_profile(operator, profile) is True
    assert await authz.can_mutate_profile(operator, profile) is True
