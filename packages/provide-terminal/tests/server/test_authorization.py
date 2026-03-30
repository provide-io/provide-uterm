#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for server authorization.py — AuthorizationService capability/role checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from provide.terminal.server.auth import Principal
from provide.terminal.server.authorization import AuthorizationService
from provide.terminal.server.models import SessionDefinition

if TYPE_CHECKING:
    from provide.terminal.server.profiles import ConnectionProfile


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
    def test_viewer_has_read_caps(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["viewer"])
        caps = authz.capabilities_for(p)
        assert "session.read" in caps
        assert "session.control.create" not in caps

    def test_operator_has_create_and_connect(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["operator"])
        caps = authz.capabilities_for(p)
        assert "session.control.create" in caps
        assert "session.control.delete" not in caps

    def test_admin_has_all_caps(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["admin"])
        caps = authz.capabilities_for(p)
        assert "session.control.delete" in caps
        assert "session.control.hijack" in caps

    def test_unknown_role_contributes_nothing(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["unknown_role"])
        caps = authz.capabilities_for(p)
        assert len(caps) == 0


class TestCanReadSession:
    def test_viewer_can_read_public(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["viewer"])
        s = _session(visibility="public")
        assert authz.can_read_session(p, s) is True

    def test_no_read_cap_returns_false(self, authz: AuthorizationService) -> None:
        # An unknown role has no caps at all — can't read anything
        p = _principal(roles=["unknown_role"])
        s = _session(visibility="public")
        assert authz.can_read_session(p, s) is False  # line 73

    def test_admin_can_read_private(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["admin"])
        s = _session(visibility="private", owner="someone_else")
        assert authz.can_read_session(p, s) is True

    def test_owner_can_read_private(self, authz: AuthorizationService) -> None:
        p = _principal(subject_id="alice", roles=["operator"])
        s = _session(visibility="private", owner="alice")
        assert authz.can_read_session(p, s) is True

    def test_operator_can_read_operator_visibility(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["operator"])
        s = _session(visibility="operator")
        assert authz.can_read_session(p, s) is True  # line 79: has_role("operator") → True

    def test_viewer_cannot_read_operator_visibility(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["viewer"])
        s = _session(visibility="operator")
        assert authz.can_read_session(p, s) is False  # line 79: has_role("operator") → False

    def test_viewer_cannot_read_private(self, authz: AuthorizationService) -> None:
        p = _principal(subject_id="bob", roles=["viewer"])
        s = _session(visibility="private", owner="alice")
        assert authz.can_read_session(p, s) is False


class TestCanMutateSession:
    def test_admin_can_mutate_any_session(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["admin"])
        s = _session(owner=None)
        assert authz.can_mutate_session(p, s, "session.control.update") is True

    def test_system_session_no_owner_blocks_operator(self, authz: AuthorizationService) -> None:
        # session.owner is None — system-managed; operators cannot mutate
        p = _principal(roles=["operator"])
        s = _session(owner=None)
        assert authz.can_mutate_session(p, s, "session.control.update") is False  # line 97

    def test_operator_can_mutate_owned_session(self, authz: AuthorizationService) -> None:
        p = _principal(subject_id="alice", roles=["operator"])
        s = _session(owner="alice")
        assert authz.can_mutate_session(p, s, "session.control.update") is True  # line 102

    def test_operator_cannot_mutate_others_session(self, authz: AuthorizationService) -> None:
        p = _principal(subject_id="bob", roles=["operator"])
        s = _session(owner="alice")
        assert authz.can_mutate_session(p, s, "session.control.update") is False

    def test_missing_capability_blocks_admin(self, authz: AuthorizationService) -> None:
        # Even admin needs the capability in the map — delete is admin-only
        p = _principal(roles=["operator"])
        s = _session(owner="operator_user")
        assert authz.can_mutate_session(p, s, "session.control.delete") is False


class TestResolveBrowserRole:
    def test_admin_principal_gets_admin_role(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["admin"])
        s = _session(visibility="public")
        assert authz.resolve_browser_role(p, s) == "admin"

    def test_operator_gets_operator_role(self, authz: AuthorizationService) -> None:
        p = _principal(subject_id="op", roles=["operator"])
        s = _session(visibility="public", owner=None)  # operator but not owner
        assert authz.resolve_browser_role(p, s) == "operator"  # line 106

    def test_owner_gets_operator_role(self, authz: AuthorizationService) -> None:
        p = _principal(subject_id="alice", roles=["operator"])
        s = _session(visibility="public", owner="alice")
        # is_owner → True, but not admin → resolve to "operator"
        role = authz.resolve_browser_role(p, s)
        assert role in {"operator", "admin"}  # owner with operator role

    def test_viewer_gets_viewer_role(self, authz: AuthorizationService) -> None:
        p = _principal(roles=["viewer"])
        s = _session(visibility="public")
        assert authz.resolve_browser_role(p, s) == "viewer"

    def test_no_read_access_gets_viewer(self, authz: AuthorizationService) -> None:
        # Unknown role: no caps → can_read_session = False → viewer
        p = _principal(roles=["unknown"])
        s = _session(visibility="public")
        assert authz.resolve_browser_role(p, s) == "viewer"


# ── Profile authorization ─────────────────────────────────────────────────


def _make_test_profile(owner: str, visibility: str = "private") -> ConnectionProfile:
    """Return a minimal ConnectionProfile for auth tests."""
    import time

    from provide.terminal.server.profiles import ConnectionProfile

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


def test_can_read_own_private_profile() -> None:
    authz = AuthorizationService()
    principal = _principal("alice", roles=["operator"])
    profile = _make_test_profile(owner="alice", visibility="private")
    assert authz.can_read_profile(principal, profile) is True


def test_cannot_read_other_private_profile() -> None:
    authz = AuthorizationService()
    principal = _principal("alice", roles=["operator"])
    profile = _make_test_profile(owner="bob", visibility="private")
    assert authz.can_read_profile(principal, profile) is False


def test_can_read_shared_profile_as_non_owner() -> None:
    authz = AuthorizationService()
    principal = _principal("alice", roles=["operator"])
    profile = _make_test_profile(owner="bob", visibility="shared")
    assert authz.can_read_profile(principal, profile) is True


def test_admin_can_read_any_profile() -> None:
    authz = AuthorizationService()
    principal = _principal("admin", roles=["admin"])
    profile = _make_test_profile(owner="bob", visibility="private")
    assert authz.can_read_profile(principal, profile) is True


def test_can_mutate_own_profile() -> None:
    authz = AuthorizationService()
    principal = _principal("alice", roles=["operator"])
    profile = _make_test_profile(owner="alice")
    assert authz.can_mutate_profile(principal, profile) is True


def test_cannot_mutate_other_profile() -> None:
    authz = AuthorizationService()
    principal = _principal("alice", roles=["operator"])
    profile = _make_test_profile(owner="bob")
    assert authz.can_mutate_profile(principal, profile) is False


def test_admin_can_mutate_any_profile() -> None:
    authz = AuthorizationService()
    principal = _principal("admin", roles=["admin"])
    profile = _make_test_profile(owner="bob")
    assert authz.can_mutate_profile(principal, profile) is True
