#
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tenant-scoped runtime graphical-target REST API."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.server.authorization import AuthorizationService
from provide.uterm.server.bridge.identity import Principal
from provide.uterm.server.graphical import GraphicalTargetScope
from provide.uterm.server.graphical.targets import (
    GraphicalTargetAlreadyExistsError,
    GraphicalTargetClosedError,
    GraphicalTargetForbiddenError,
    GraphicalTargetImmutableError,
    GraphicalTargetNotFoundError,
    GraphicalTargetTransactionError,
)
from provide.uterm.server.routes.graphical_targets import create_graphical_targets_router


def _client(principal: Principal, registry: AsyncMock | None = None) -> tuple[TestClient, AsyncMock]:
    app = FastAPI()
    app.include_router(create_graphical_targets_router(), prefix="/api")
    app.state.uterm_authz = AuthorizationService()
    app.state.uterm_graphical_target_registry = registry or AsyncMock()

    @app.middleware("http")
    async def set_principal(request, call_next):
        request.state.uterm_principal = principal
        return await call_next(request)

    return TestClient(app), app.state.uterm_graphical_target_registry


def _payload() -> dict[str, object]:
    return {"target_id": "desktop", "endpoint": "dns:///desktop.internal:443"}


def test_create_injects_principal_tenant_and_never_accepts_tenant_from_body() -> None:
    client, registry = _client(Principal(subject_id="alice", tenant_id="tenant-a", roles=frozenset({"operator"})))
    registry.create.side_effect = lambda _scope, target: target

    response = client.post("/api/graphical-targets", json=_payload())

    assert response.status_code == 201
    scope, target = registry.create.await_args.args
    assert scope == GraphicalTargetScope.tenant("tenant-a")
    assert target.tenant_id == "tenant-a"
    assert response.json()["tenant_id"] == "tenant-a"
    assert client.post("/api/graphical-targets", json={**_payload(), "tenant_id": "tenant-b"}).status_code == 422


def test_response_redacts_secret_reference_locators() -> None:
    client, registry = _client(Principal(subject_id="alice", tenant_id="tenant-a", roles=frozenset({"operator"})))
    registry.create.side_effect = lambda _scope, target: target

    response = client.post(
        "/api/graphical-targets",
        json={
            **_payload(),
            "ca_secret_ref": "env:HIGHLY_SENSITIVE_SECRET_NAME",  # pragma: allowlist secret
        },
    )

    assert response.status_code == 201
    assert "HIGHLY_SENSITIVE_SECRET_NAME" not in response.text
    assert "ca_secret_ref" not in response.json()


def test_routes_require_tenant_and_explicit_capability() -> None:
    no_tenant, registry = _client(Principal(subject_id="admin", roles=frozenset({"admin"})))
    viewer, _ = _client(Principal(subject_id="viewer", tenant_id="tenant-a", roles=frozenset({"viewer"})), registry)

    assert no_tenant.get("/api/graphical-targets").status_code == 403
    assert viewer.post("/api/graphical-targets", json=_payload()).status_code == 403
    registry.list.assert_not_awaited()
    registry.create.assert_not_awaited()


def test_list_is_bounded_and_uses_tenant_scope() -> None:
    client, registry = _client(Principal(subject_id="alice", tenant_id="tenant-a", roles=frozenset({"viewer"})))
    registry.list.return_value = []

    response = client.get("/api/graphical-targets?limit=25&offset=10")

    assert response.status_code == 200
    registry.list.assert_awaited_once_with(GraphicalTargetScope.tenant("tenant-a"))
    assert response.json() == {"items": [], "limit": 25, "offset": 10, "total": 0}
    assert client.get("/api/graphical-targets?limit=201").status_code == 422


def test_registry_errors_have_stable_public_codes() -> None:
    client, registry = _client(Principal(subject_id="alice", tenant_id="tenant-a", roles=frozenset({"operator"})))
    registry.delete.side_effect = GraphicalTargetImmutableError("internal detail")

    response = client.delete("/api/graphical-targets/static")

    assert response.status_code == 409
    assert response.json() == {
        "detail": {"code": "graphical_target_immutable", "message": "static graphical target is immutable"}
    }


def test_get_update_and_delete_use_tenant_scope() -> None:
    client, registry = _client(Principal(subject_id="alice", tenant_id="tenant-a", roles=frozenset({"operator"})))
    from provide.uterm.server.config_schema_graphical import GraphicalTargetDefinition

    target = GraphicalTargetDefinition(**_payload(), tenant_id="tenant-a")
    registry.get.return_value = target
    registry.update.side_effect = lambda _scope, updated: updated

    assert client.get("/api/graphical-targets/desktop").status_code == 200
    assert client.put("/api/graphical-targets/desktop", json=_payload()).status_code == 200
    assert client.delete("/api/graphical-targets/desktop").status_code == 204
    scope = GraphicalTargetScope.tenant("tenant-a")
    registry.get.assert_awaited_once_with(scope, "desktop")
    assert registry.update.await_args.args[0] == scope
    assert registry.update.await_args.args[1].tenant_id == "tenant-a"
    registry.delete.assert_awaited_once_with(scope, "desktop")


def test_get_conceals_missing_target_and_validates_path() -> None:
    client, registry = _client(Principal(subject_id="alice", tenant_id="tenant-a", roles=frozenset({"viewer"})))
    registry.get.return_value = None

    assert client.get("/api/graphical-targets/missing").status_code == 404
    assert client.get("/api/graphical-targets/bad!id").status_code == 422


@pytest.mark.parametrize("operation", ["list", "get", "create", "update"])
def test_faulty_registry_cross_tenant_results_are_concealed_and_not_serialized(operation: str) -> None:
    client, registry = _client(Principal(subject_id="alice", tenant_id="tenant-a", roles=frozenset({"operator"})))
    from provide.uterm.server.config_schema_graphical import GraphicalTargetDefinition

    foreign = GraphicalTargetDefinition(**_payload(), tenant_id="tenant-b", audit_labels={"secret": "foreign-data"})
    getattr(registry, operation).return_value = [foreign] if operation == "list" else foreign

    if operation == "list":
        response = client.get("/api/graphical-targets")
    elif operation == "get":
        response = client.get("/api/graphical-targets/desktop")
    elif operation == "create":
        response = client.post("/api/graphical-targets", json=_payload())
    else:
        response = client.put("/api/graphical-targets/desktop", json=_payload())

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "graphical_target_not_found"
    assert "foreign-data" not in response.text


def test_faulty_registry_mismatch_audit_contains_no_object_data() -> None:
    client, registry = _client(Principal(subject_id="alice", tenant_id="tenant-a", roles=frozenset({"viewer"})))
    from provide.uterm.server.config_schema_graphical import GraphicalTargetDefinition

    registry.get.return_value = GraphicalTargetDefinition(
        **_payload(), tenant_id="tenant-b", audit_labels={"secret": "foreign-data"}
    )
    with patch("provide.uterm.server.routes.graphical_targets.audit_event") as audit:
        response = client.get("/api/graphical-targets/desktop")

    assert response.status_code == 404
    _, kwargs = audit.call_args
    assert kwargs["detail"] == {"expected_tenant_id": "tenant-a"}
    assert "foreign-data" not in repr(audit.call_args)


def test_update_rejects_mismatched_id_and_invalid_definition() -> None:
    client, registry = _client(Principal(subject_id="alice", tenant_id="tenant-a", roles=frozenset({"operator"})))

    mismatch = client.put("/api/graphical-targets/desktop", json={**_payload(), "target_id": "other"})
    invalid = client.post("/api/graphical-targets", json={"target_id": "desktop", "endpoint": "secret"})

    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "target_id_mismatch"
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "graphical_target_invalid"
    registry.update.assert_not_awaited()
    registry.create.assert_not_awaited()


def test_registry_unavailable_is_stable_503() -> None:
    client, _registry_mock = _client(Principal(subject_id="alice", tenant_id="tenant-a", roles=frozenset({"viewer"})))
    client.app.state.uterm_graphical_target_registry = None

    response = client.get("/api/graphical-targets")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "graphical_target_unavailable"


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (GraphicalTargetNotFoundError(), 404, "graphical_target_not_found"),
        (GraphicalTargetForbiddenError(), 404, "graphical_target_not_found"),
        (GraphicalTargetAlreadyExistsError(), 409, "graphical_target_exists"),
        (GraphicalTargetTransactionError(), 409, "graphical_target_conflict"),
        (GraphicalTargetClosedError(), 503, "graphical_target_unavailable"),
        (RuntimeError("sensitive backend detail"), 503, "graphical_target_backend_error"),
    ],
)
def test_registry_failures_are_redacted(exc: Exception, status: int, code: str) -> None:
    client, registry = _client(Principal(subject_id="alice", tenant_id="tenant-a", roles=frozenset({"viewer"})))
    registry.list.side_effect = exc

    response = client.get("/api/graphical-targets")

    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    assert "sensitive" not in response.text


@pytest.mark.parametrize(("http_method", "registry_method"), [("get", "get"), ("post", "create"), ("put", "update")])
def test_operation_backend_failures_are_mapped(http_method: str, registry_method: str) -> None:
    client, registry = _client(Principal(subject_id="alice", tenant_id="tenant-a", roles=frozenset({"operator"})))
    getattr(registry, registry_method).side_effect = RuntimeError("backend")

    if http_method == "get":
        response = client.get("/api/graphical-targets/desktop")
    elif http_method == "post":
        response = client.post("/api/graphical-targets", json=_payload())
    else:
        response = client.put("/api/graphical-targets/desktop", json=_payload())

    assert response.status_code == 503
