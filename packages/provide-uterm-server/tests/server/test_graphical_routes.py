#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""REST + wiring tests for the graphical-target routes.

Covers every HTTP handler + error branch (404 / 409 / 422 / pagination /
tenant_managed / secret-stripping / tenant isolation). Error-code→HTTP mapping,
config-seed conversion, and body-parse helpers live in
``test_graphical_routes_units.py``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.config_schema import GraphicalTargetConfig

ACME = {"x-uterm-principal": "u1", "x-uterm-role": "operator", "x-uterm-tenant": "acme"}
ACME_VIEWER = {"x-uterm-principal": "u1", "x-uterm-role": "viewer", "x-uterm-tenant": "acme"}
OTHER = {"x-uterm-principal": "u2", "x-uterm-role": "operator", "x-uterm-tenant": "other"}
NO_TENANT = {"x-uterm-principal": "u3", "x-uterm-role": "operator"}


def _make_client(targets: list[GraphicalTargetConfig] | None = None) -> TestClient:
    cfg = default_server_config()
    cfg.auth.mode = "header"
    cfg.auth.header_mode_acknowledged = True
    cfg.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    if targets is not None:
        cfg.graphical_targets = targets
    return TestClient(create_server_app(cfg))


def _seeded_client() -> TestClient:
    return _make_client(
        [
            GraphicalTargetConfig(
                target_id="gt-console",
                tenant_id="acme",
                protocol="rfb",
                target_address="vm.local:5900",
                name="Console",
                width=800,
                height=600,
                enabled=True,
            ),
            GraphicalTargetConfig(target_id="gt-mem", tenant_id="acme", protocol="memory", name="Mem", enabled=True),
        ]
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestList:
    def test_list_ok(self) -> None:
        resp = _seeded_client().get("/api/graphical-targets", headers=ACME)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert body["limit"] == 100
        assert body["offset"] == 0
        assert [t["target_id"] for t in body["items"]] == ["gt-console", "gt-mem"]

    def test_list_no_capability_forbidden(self) -> None:
        # viewer has graphical.target.read, so use a role lacking it: strip via
        # an unknown role -> filtered to viewer? Use no-tenant instead here.
        resp = _seeded_client().get("/api/graphical-targets", headers=NO_TENANT)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "graphical target access denied"

    def test_list_cross_tenant_empty(self) -> None:
        resp = _seeded_client().get("/api/graphical-targets", headers=OTHER)
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    def test_list_pagination(self) -> None:
        resp = _seeded_client().get("/api/graphical-targets?limit=1&offset=1", headers=ACME)
        body = resp.json()
        assert body["limit"] == 1
        assert body["offset"] == 1
        assert [t["target_id"] for t in body["items"]] == ["gt-mem"]

    def test_list_offset_beyond_total(self) -> None:
        resp = _seeded_client().get("/api/graphical-targets?offset=99", headers=ACME)
        assert resp.json()["items"] == []

    def test_list_bad_limit_non_int(self) -> None:
        resp = _seeded_client().get("/api/graphical-targets?limit=abc", headers=ACME)
        assert resp.status_code == 422
        assert resp.json()["detail"]["message"] == "limit must be between 1 and 200"

    def test_list_limit_too_large(self) -> None:
        resp = _seeded_client().get("/api/graphical-targets?limit=201", headers=ACME)
        assert resp.status_code == 422

    def test_list_limit_too_small(self) -> None:
        resp = _seeded_client().get("/api/graphical-targets?limit=0", headers=ACME)
        assert resp.status_code == 422

    def test_list_bad_offset(self) -> None:
        resp = _seeded_client().get("/api/graphical-targets?offset=-1", headers=ACME)
        assert resp.status_code == 422
        assert resp.json()["detail"]["message"] == "offset must be non-negative"

    def test_list_offset_non_int(self) -> None:
        resp = _seeded_client().get("/api/graphical-targets?offset=xx", headers=ACME)
        assert resp.status_code == 422

    def test_list_blank_query_args_use_defaults(self) -> None:
        resp = _seeded_client().get("/api/graphical-targets?limit=&offset=", headers=ACME)
        assert resp.status_code == 200
        assert resp.json()["limit"] == 100

    def test_list_closed_registry_503(self) -> None:
        client = _seeded_client()
        client.app.state.uterm_graphical_targets.close()
        resp = client.get("/api/graphical-targets", headers=ACME)
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "graphical_target_unavailable"


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


class TestGet:
    def test_get_ok_strips_secret(self) -> None:
        resp = _seeded_client().get("/api/graphical-targets/gt-console", headers=ACME)
        assert resp.status_code == 200
        assert "secret" not in resp.json()  # pragma: allowlist secret

    def test_get_not_found(self) -> None:
        resp = _seeded_client().get("/api/graphical-targets/nope", headers=ACME)
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "graphical_target_not_found"

    def test_get_cross_tenant_not_found(self) -> None:
        resp = _seeded_client().get("/api/graphical-targets/gt-console", headers=OTHER)
        assert resp.status_code == 404

    def test_get_no_capability(self) -> None:
        resp = _seeded_client().get("/api/graphical-targets/gt-console", headers=NO_TENANT)
        assert resp.status_code == 403

    def test_get_closed_registry_503(self) -> None:
        client = _seeded_client()
        client.app.state.uterm_graphical_targets.close()
        resp = client.get("/api/graphical-targets/gt-console", headers=ACME)
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestCreate:
    def test_create_ok(self) -> None:
        resp = _make_client([]).post(
            "/api/graphical-targets",
            json={
                "protocol": "rfb",
                "endpoint": "1.2.3.4:5901",
                "secret": "s",
                "display_name": "New",
            },  # pragma: allowlist secret
            headers=ACME,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["tenant_id"] == "acme"
        assert body["created_by"] == "u1"
        assert body["is_system"] is False
        assert body["target_id"].startswith("gt-")
        assert "secret" not in body  # pragma: allowlist secret

    def test_create_default_display_name(self) -> None:
        resp = _make_client([]).post(
            "/api/graphical-targets",
            json={"protocol": "rfb", "endpoint": "1.2.3.4:5901"},
            headers=ACME,
        )
        assert resp.json()["display_name"] == "graphical-target"

    def test_create_viewer_forbidden(self) -> None:
        resp = _make_client([]).post(
            "/api/graphical-targets",
            json={"protocol": "memory"},
            headers=ACME_VIEWER,
        )
        assert resp.status_code == 403

    def test_create_no_tenant_forbidden(self) -> None:
        resp = _make_client([]).post("/api/graphical-targets", json={"protocol": "memory"}, headers=NO_TENANT)
        assert resp.status_code == 403

    def test_create_rejects_tenant_id(self) -> None:
        resp = _make_client([]).post(
            "/api/graphical-targets",
            json={"tenant_id": "acme", "protocol": "memory"},
            headers=ACME,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "tenant_managed"

    def test_create_rejects_non_string_tenant_id(self) -> None:
        # A non-string tenant_id is still "present" → tenant_managed (the
        # parse helper skips assigning a non-string value).
        resp = _make_client([]).post(
            "/api/graphical-targets",
            json={"tenant_id": 123, "protocol": "memory"},
            headers=ACME,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "tenant_managed"

    def test_create_rejects_target_id(self) -> None:
        resp = _make_client([]).post(
            "/api/graphical-targets",
            json={"target_id": "gt-mine", "protocol": "memory"},
            headers=ACME,
        )
        assert resp.status_code == 422
        assert "server-assigned" in resp.json()["detail"]["message"]

    def test_create_unknown_key_rejected(self) -> None:
        resp = _make_client([]).post(
            "/api/graphical-targets",
            json={"protocol": "memory", "bogus": 1},
            headers=ACME,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["message"] == "invalid request body"

    def test_create_wrong_type_field(self) -> None:
        resp = _make_client([]).post(
            "/api/graphical-targets",
            json={"protocol": "memory", "width": "not-an-int"},
            headers=ACME,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["message"] == "width must be an integer"

    def test_create_invalid_definition(self) -> None:
        resp = _make_client([]).post(
            "/api/graphical-targets",
            json={"protocol": "rfb", "endpoint": "no-port-here"},
            headers=ACME,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "graphical_target_invalid"

    def test_create_bad_json_body(self) -> None:
        resp = _make_client([]).post(
            "/api/graphical-targets",
            content=b"not json",
            headers={**ACME, "content-type": "application/json"},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["message"] == "invalid request body"

    def test_create_non_object_body(self) -> None:
        resp = _make_client([]).post("/api/graphical-targets", json=[1, 2, 3], headers=ACME)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def _create_runtime(client: TestClient) -> str:
    resp = client.post(
        "/api/graphical-targets",
        json={"protocol": "rfb", "endpoint": "1.2.3.4:5901", "display_name": "Orig"},
        headers=ACME,
    )
    return resp.json()["target_id"]


class TestUpdate:
    def test_update_ok(self) -> None:
        client = _make_client([])
        tid = _create_runtime(client)
        resp = client.put(
            f"/api/graphical-targets/{tid}",
            json={"protocol": "rfb", "endpoint": "9.9.9.9:6000"},
            headers=ACME,
        )
        assert resp.status_code == 200
        assert resp.json()["endpoint"] == "9.9.9.9:6000"
        # display_name preserved from the existing target when blank
        assert resp.json()["display_name"] == "Orig"

    def test_update_not_found(self) -> None:
        resp = _make_client([]).put(
            "/api/graphical-targets/gt-missing",
            json={"protocol": "memory"},
            headers=ACME,
        )
        assert resp.status_code == 404

    def test_update_target_id_mismatch(self) -> None:
        client = _make_client([])
        tid = _create_runtime(client)
        resp = client.put(
            f"/api/graphical-targets/{tid}",
            json={"target_id": "gt-other", "protocol": "memory"},
            headers=ACME,
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "target_id_mismatch"

    def test_update_rejects_tenant_id(self) -> None:
        client = _make_client([])
        tid = _create_runtime(client)
        resp = client.put(
            f"/api/graphical-targets/{tid}",
            json={"tenant_id": "acme", "protocol": "memory"},
            headers=ACME,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "tenant_managed"

    def test_update_static_immutable(self) -> None:
        resp = _seeded_client().put(
            "/api/graphical-targets/gt-mem",
            json={"protocol": "memory"},
            headers=ACME,
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "graphical_target_immutable"

    def test_update_no_capability(self) -> None:
        resp = _seeded_client().put("/api/graphical-targets/gt-mem", json={"protocol": "memory"}, headers=NO_TENANT)
        assert resp.status_code == 403

    def test_update_bad_body(self) -> None:
        resp = _make_client([]).put(
            "/api/graphical-targets/gt-x",
            json={"protocol": "memory", "height": "x"},
            headers=ACME,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["message"] == "height must be an integer"

    def test_update_unknown_key(self) -> None:
        resp = _make_client([]).put(
            "/api/graphical-targets/gt-x",
            json={"protocol": "memory", "nope": 1},
            headers=ACME,
        )
        assert resp.status_code == 422

    def test_update_keeps_supplied_display_name(self) -> None:
        client = _make_client([])
        tid = _create_runtime(client)
        resp = client.put(
            f"/api/graphical-targets/{tid}",
            json={"protocol": "rfb", "endpoint": "9.9.9.9:6000", "display_name": "Renamed"},
            headers=ACME,
        )
        assert resp.json()["display_name"] == "Renamed"

    def test_update_closed_registry(self) -> None:
        client = _make_client([])
        tid = _create_runtime(client)
        client.app.state.uterm_graphical_targets.close()
        resp = client.put(f"/api/graphical-targets/{tid}", json={"protocol": "memory"}, headers=ACME)
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_ok(self) -> None:
        client = _make_client([])
        tid = _create_runtime(client)
        resp = client.delete(f"/api/graphical-targets/{tid}", headers=ACME)
        assert resp.status_code == 204
        assert client.get(f"/api/graphical-targets/{tid}", headers=ACME).status_code == 404

    def test_delete_not_found(self) -> None:
        resp = _make_client([]).delete("/api/graphical-targets/gt-missing", headers=ACME)
        assert resp.status_code == 404

    def test_delete_static_immutable(self) -> None:
        resp = _seeded_client().delete("/api/graphical-targets/gt-mem", headers=ACME)
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "graphical_target_immutable"

    def test_delete_static_cross_tenant_not_found(self) -> None:
        resp = _seeded_client().delete("/api/graphical-targets/gt-console", headers=OTHER)
        assert resp.status_code == 404

    def test_delete_no_capability(self) -> None:
        resp = _seeded_client().delete("/api/graphical-targets/gt-mem", headers=NO_TENANT)
        assert resp.status_code == 403
