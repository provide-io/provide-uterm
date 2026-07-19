#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""InMemoryGraphicalTargetRegistry tests: tenant isolation, static immutability,
closed state, static+runtime merge."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from provide.uterm.server.graphical_targets import (
    GraphicalTargetDefinition,
    GraphicalTargetError,
    GraphicalTargetErrorCode,
    InMemoryGraphicalTargetRegistry,
    scope_for_tenant,
    system_scope,
)


def _fixed_clock() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def _def(**kw: object) -> GraphicalTargetDefinition:
    base: dict[str, object] = {"target_id": "gt-1", "tenant_id": "acme", "protocol": "memory"}
    base.update(kw)
    return GraphicalTargetDefinition(**base)  # type: ignore[arg-type]


def _registry_with_static() -> InMemoryGraphicalTargetRegistry:
    reg = InMemoryGraphicalTargetRegistry(now=_fixed_clock)
    reg.add_static(_def(target_id="gt-static", tenant_id="acme", protocol="memory", display_name="Static"))
    return reg


class TestRegistry:
    def test_add_static_sets_system_and_validates(self) -> None:
        reg = _registry_with_static()
        scope, _ = scope_for_tenant("acme")
        assert scope is not None
        got = reg.get(scope, "gt-static")
        assert got is not None
        assert got.is_system is True

    def test_add_static_validate_error(self) -> None:
        reg = InMemoryGraphicalTargetRegistry()
        with pytest.raises(GraphicalTargetError, match="safe identifier"):
            reg.add_static(_def(target_id="bad id"))

    def test_add_static_duplicate(self) -> None:
        reg = _registry_with_static()
        with pytest.raises(GraphicalTargetError) as exc:
            reg.add_static(_def(target_id="gt-static", protocol="memory"))
        assert exc.value.code is GraphicalTargetErrorCode.CONFLICT

    def test_create_and_get(self) -> None:
        reg = InMemoryGraphicalTargetRegistry(now=_fixed_clock)
        scope, _ = scope_for_tenant("acme")
        assert scope is not None
        created = reg.create(scope, _def(target_id="gt-x", protocol="memory"))
        assert created.created_at == _fixed_clock()
        assert reg.get(scope, "gt-x") is not None

    def test_create_default_clock(self) -> None:
        reg = InMemoryGraphicalTargetRegistry()
        scope, _ = scope_for_tenant("acme")
        assert scope is not None
        created = reg.create(scope, _def(target_id="gt-x", protocol="memory"))
        assert created.created_at.tzinfo is UTC

    def test_create_scope_denied(self) -> None:
        reg = InMemoryGraphicalTargetRegistry()
        scope, _ = scope_for_tenant("other")
        assert scope is not None
        with pytest.raises(GraphicalTargetError) as exc:
            reg.create(scope, _def(target_id="gt-x", tenant_id="acme", protocol="memory"))
        assert exc.value.code is GraphicalTargetErrorCode.FORBIDDEN

    def test_create_validate_error(self) -> None:
        reg = InMemoryGraphicalTargetRegistry()
        scope, _ = scope_for_tenant("acme")
        assert scope is not None
        with pytest.raises(GraphicalTargetError) as exc:
            reg.create(scope, _def(target_id="gt-x", protocol="rfb", endpoint=None))
        assert exc.value.code is GraphicalTargetErrorCode.INVALID

    def test_create_already_exists_runtime(self) -> None:
        reg = InMemoryGraphicalTargetRegistry()
        scope, _ = scope_for_tenant("acme")
        assert scope is not None
        reg.create(scope, _def(target_id="gt-x", protocol="memory"))
        with pytest.raises(GraphicalTargetError) as exc:
            reg.create(scope, _def(target_id="gt-x", protocol="memory"))
        assert exc.value.code is GraphicalTargetErrorCode.ALREADY_EXISTS

    def test_create_already_exists_static(self) -> None:
        reg = _registry_with_static()
        scope, _ = scope_for_tenant("acme")
        assert scope is not None
        with pytest.raises(GraphicalTargetError) as exc:
            reg.create(scope, _def(target_id="gt-static", protocol="memory"))
        assert exc.value.code is GraphicalTargetErrorCode.ALREADY_EXISTS

    def test_get_cross_tenant_denied(self) -> None:
        reg = _registry_with_static()
        other, _ = scope_for_tenant("other")
        assert other is not None
        assert reg.get(other, "gt-static") is None

    def test_get_runtime_cross_tenant_denied(self) -> None:
        reg = InMemoryGraphicalTargetRegistry()
        acme, _ = scope_for_tenant("acme")
        other, _ = scope_for_tenant("other")
        assert acme is not None and other is not None
        reg.create(acme, _def(target_id="gt-x", protocol="memory"))
        assert reg.get(other, "gt-x") is None

    def test_get_unknown_returns_none(self) -> None:
        reg = InMemoryGraphicalTargetRegistry()
        scope, _ = scope_for_tenant("acme")
        assert scope is not None
        assert reg.get(scope, "nope") is None

    def test_list_merges_static_and_runtime_sorted(self) -> None:
        reg = _registry_with_static()
        scope, _ = scope_for_tenant("acme")
        assert scope is not None
        reg.create(scope, _def(target_id="gt-aaa", protocol="memory"))
        rows = reg.list(scope)
        assert [r.target_id for r in rows] == ["gt-aaa", "gt-static"]

    def test_list_static_wins_on_collision(self) -> None:
        reg = _registry_with_static()
        scope, _ = scope_for_tenant("acme")
        assert scope is not None
        # Inject a runtime entry colliding with the static id (white-box).
        reg._runtime["gt-static"] = _def(target_id="gt-static", protocol="memory", display_name="Runtime")
        rows = reg.list(scope)
        row = next(r for r in rows if r.target_id == "gt-static")
        assert row.display_name == "Static"

    def test_list_filters_other_tenants(self) -> None:
        reg = _registry_with_static()
        other, _ = scope_for_tenant("other")
        assert other is not None
        assert reg.list(other) == []

    def test_list_skips_runtime_of_other_tenant(self) -> None:
        reg = InMemoryGraphicalTargetRegistry()
        acme, _ = scope_for_tenant("acme")
        assert acme is not None
        reg.create(acme, _def(target_id="gt-a", tenant_id="acme", protocol="memory"))
        # White-box: a runtime entry owned by a different tenant is filtered out.
        reg._runtime["gt-b"] = _def(target_id="gt-b", tenant_id="other", protocol="memory")
        assert [r.target_id for r in reg.list(acme)] == ["gt-a"]

    def test_update_success_preserves_created(self) -> None:
        reg = InMemoryGraphicalTargetRegistry(now=_fixed_clock)
        scope, _ = scope_for_tenant("acme")
        assert scope is not None
        reg.create(scope, _def(target_id="gt-x", protocol="memory", created_by="alice"))
        updated = reg.update(scope, _def(target_id="gt-x", protocol="memory", display_name="v2"))
        assert updated.created_by == "alice"
        assert updated.updated_at == _fixed_clock()

    def test_update_scope_denied(self) -> None:
        reg = InMemoryGraphicalTargetRegistry()
        scope, _ = scope_for_tenant("other")
        assert scope is not None
        with pytest.raises(GraphicalTargetError) as exc:
            reg.update(scope, _def(target_id="gt-x", tenant_id="acme", protocol="memory"))
        assert exc.value.code is GraphicalTargetErrorCode.FORBIDDEN

    def test_update_validate_error(self) -> None:
        reg = InMemoryGraphicalTargetRegistry()
        scope, _ = scope_for_tenant("acme")
        assert scope is not None
        with pytest.raises(GraphicalTargetError) as exc:
            reg.update(scope, _def(target_id="gt-x", protocol="rfb", endpoint=None))
        assert exc.value.code is GraphicalTargetErrorCode.INVALID

    def test_update_static_immutable(self) -> None:
        reg = _registry_with_static()
        scope, _ = scope_for_tenant("acme")
        assert scope is not None
        with pytest.raises(GraphicalTargetError) as exc:
            reg.update(scope, _def(target_id="gt-static", protocol="memory"))
        assert exc.value.code is GraphicalTargetErrorCode.IMMUTABLE

    def test_update_not_found(self) -> None:
        reg = InMemoryGraphicalTargetRegistry()
        scope, _ = scope_for_tenant("acme")
        assert scope is not None
        with pytest.raises(GraphicalTargetError) as exc:
            reg.update(scope, _def(target_id="gt-missing", protocol="memory"))
        assert exc.value.code is GraphicalTargetErrorCode.NOT_FOUND

    def test_update_current_tenant_mismatch_defensive(self) -> None:
        reg = InMemoryGraphicalTargetRegistry()
        scope, _ = scope_for_tenant("acme")
        assert scope is not None
        # White-box: a runtime entry whose stored tenant differs from the scope.
        reg._runtime["gt-x"] = _def(target_id="gt-x", tenant_id="other", protocol="memory")
        with pytest.raises(GraphicalTargetError) as exc:
            reg.update(scope, _def(target_id="gt-x", tenant_id="acme", protocol="memory"))
        assert exc.value.code is GraphicalTargetErrorCode.FORBIDDEN

    def test_delete_runtime(self) -> None:
        reg = InMemoryGraphicalTargetRegistry()
        scope, _ = scope_for_tenant("acme")
        assert scope is not None
        reg.create(scope, _def(target_id="gt-x", protocol="memory"))
        reg.delete(scope, "gt-x")
        assert reg.get(scope, "gt-x") is None

    def test_delete_static_immutable(self) -> None:
        reg = _registry_with_static()
        scope, _ = scope_for_tenant("acme")
        assert scope is not None
        with pytest.raises(GraphicalTargetError) as exc:
            reg.delete(scope, "gt-static")
        assert exc.value.code is GraphicalTargetErrorCode.IMMUTABLE

    def test_delete_static_cross_tenant_forbidden(self) -> None:
        reg = _registry_with_static()
        other, _ = scope_for_tenant("other")
        assert other is not None
        with pytest.raises(GraphicalTargetError) as exc:
            reg.delete(other, "gt-static")
        assert exc.value.code is GraphicalTargetErrorCode.FORBIDDEN

    def test_delete_not_found(self) -> None:
        reg = InMemoryGraphicalTargetRegistry()
        scope, _ = scope_for_tenant("acme")
        assert scope is not None
        with pytest.raises(GraphicalTargetError) as exc:
            reg.delete(scope, "gt-missing")
        assert exc.value.code is GraphicalTargetErrorCode.NOT_FOUND

    def test_delete_runtime_cross_tenant_defensive(self) -> None:
        reg = InMemoryGraphicalTargetRegistry()
        scope, _ = scope_for_tenant("acme")
        assert scope is not None
        reg._runtime["gt-x"] = _def(target_id="gt-x", tenant_id="other", protocol="memory")
        with pytest.raises(GraphicalTargetError) as exc:
            reg.delete(scope, "gt-x")
        assert exc.value.code is GraphicalTargetErrorCode.FORBIDDEN

    def test_closed_registry_rejects(self) -> None:
        reg = _registry_with_static()
        reg.close()
        scope, _ = scope_for_tenant("acme")
        assert scope is not None
        with pytest.raises(GraphicalTargetError) as exc:
            reg.list(scope)
        assert exc.value.code is GraphicalTargetErrorCode.CLOSED

    def test_invalid_scope_rejected(self) -> None:
        from provide.uterm.server.graphical_targets import GraphicalTargetScope

        reg = InMemoryGraphicalTargetRegistry()
        bad = GraphicalTargetScope(tenant_id=None, is_system=False)
        with pytest.raises(GraphicalTargetError) as exc:
            reg.list(bad)
        assert exc.value.code is GraphicalTargetErrorCode.FORBIDDEN

    def test_system_scope_sees_all_tenants(self) -> None:
        reg = _registry_with_static()
        acme, _ = scope_for_tenant("acme")
        assert acme is not None
        reg.create(acme, _def(target_id="gt-y", tenant_id="acme", protocol="memory"))
        rows = reg.list(system_scope())
        assert {r.target_id for r in rows} == {"gt-static", "gt-y"}
