#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Graphical-route unit tests: error-code→HTTP mapping, config-seed conversion
(SeedGraphicalTargets / ToGraphicalTargetDefinition), body-parse helpers."""

from __future__ import annotations

import pytest

from provide.uterm.server import default_server_config
from provide.uterm.server.config_schema import GraphicalTargetConfig
from provide.uterm.server.graphical_routes import (
    _clamp_dimension,
    _config_to_definition,
    _get_int,
    _get_string,
    _map_route_error,
    _principal,
    seed_graphical_targets,
)
from provide.uterm.server.graphical_targets import (
    GraphicalTargetError,
    GraphicalTargetErrorCode,
)

# ---------------------------------------------------------------------------
# Error mapping (every code)
# ---------------------------------------------------------------------------


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("code", "status", "err_code"),
        [
            (GraphicalTargetErrorCode.ALREADY_EXISTS, 409, "graphical_target_exists"),
            (GraphicalTargetErrorCode.IMMUTABLE, 409, "graphical_target_immutable"),
            (GraphicalTargetErrorCode.CONFLICT, 409, "graphical_target_conflict"),
            (GraphicalTargetErrorCode.INVALID, 422, "graphical_target_invalid"),
            (GraphicalTargetErrorCode.NOT_FOUND, 404, "graphical_target_not_found"),
            (GraphicalTargetErrorCode.FORBIDDEN, 404, "graphical_target_not_found"),
            (GraphicalTargetErrorCode.CLOSED, 503, "graphical_target_unavailable"),
            (GraphicalTargetErrorCode.BACKEND, 503, "graphical_target_backend_error"),
        ],
    )
    def test_map(self, code: GraphicalTargetErrorCode, status: int, err_code: str) -> None:
        exc = _map_route_error(GraphicalTargetError(code, "x"))
        assert exc.status_code == status
        assert exc.detail["code"] == err_code  # type: ignore[index]


# ---------------------------------------------------------------------------
# Config seed (SeedGraphicalTargets / ToGraphicalTargetDefinition)
# ---------------------------------------------------------------------------


class TestSeed:
    def test_seed_skips_disabled(self) -> None:
        cfg = default_server_config()
        cfg.graphical_targets = [
            GraphicalTargetConfig(target_id="gt-a", tenant_id="acme", protocol="memory", enabled=True),
            GraphicalTargetConfig(target_id="gt-b", tenant_id="acme", protocol="memory", enabled=False),
        ]
        reg = seed_graphical_targets(cfg)
        from provide.uterm.server.graphical_targets import system_scope

        ids = {t.target_id for t in reg.list(system_scope())}
        assert ids == {"gt-a"}

    def test_config_to_definition_rfb(self) -> None:
        target = GraphicalTargetConfig(
            target_id="gt-x", tenant_id="acme", protocol="RFB", target_address=" vm:5900 ", name="X"
        )
        d = _config_to_definition(target)
        assert d.protocol == "rfb"
        assert d.endpoint == "vm:5900"
        assert d.is_system and d.is_static
        assert d.display_name == "X"

    def test_config_to_definition_memory_no_endpoint(self) -> None:
        d = _config_to_definition(GraphicalTargetConfig(target_id="gt-m", protocol="memory", name="M"))
        assert d.endpoint is None

    def test_config_to_definition_blank_id_generated(self) -> None:
        d = _config_to_definition(GraphicalTargetConfig(target_id="  ", protocol="memory"))
        assert d.target_id.startswith("gt-")

    def test_config_to_definition_blank_name_uses_id(self) -> None:
        d = _config_to_definition(GraphicalTargetConfig(target_id="gt-x", protocol="memory", name="  "))
        assert d.display_name == "gt-x"

    def test_config_to_definition_unsupported_protocol(self) -> None:
        with pytest.raises(GraphicalTargetError, match="unsupported graphical target protocol"):
            _config_to_definition(GraphicalTargetConfig(target_id="gt-x", protocol="ftp"))

    def test_config_to_definition_rfb_requires_address(self) -> None:
        with pytest.raises(GraphicalTargetError, match="requires target_address"):
            _config_to_definition(GraphicalTargetConfig(target_id="gt-x", protocol="rfb", target_address=""))

    def test_config_to_definition_default_protocol(self) -> None:
        d = _config_to_definition(GraphicalTargetConfig(target_id="gt-x", protocol="", target_address="vm:1"))
        assert d.protocol == "rfb"

    def test_seed_invalid_target_fails(self) -> None:
        cfg = default_server_config()
        cfg.graphical_targets = [
            GraphicalTargetConfig(target_id="gt-x", protocol="rfb", target_address="", enabled=True)
        ]
        with pytest.raises(GraphicalTargetError):
            seed_graphical_targets(cfg)


class TestBodyHelpers:
    def test_get_string_absent_returns_fallback(self) -> None:
        assert _get_string({}, "k", "fb") == "fb"

    def test_get_string_null_returns_fallback(self) -> None:
        assert _get_string({"k": None}, "k", "fb") == "fb"

    def test_get_string_value(self) -> None:
        assert _get_string({"k": "v"}, "k", "fb") == "v"

    def test_get_string_wrong_type(self) -> None:
        with pytest.raises(GraphicalTargetError, match="k must be a string"):
            _get_string({"k": 5}, "k", "fb")

    def test_get_int_absent_returns_fallback(self) -> None:
        assert _get_int({}, "k", 42) == 42

    def test_get_int_null_returns_fallback(self) -> None:
        assert _get_int({"k": None}, "k", 42) == 42

    def test_get_int_bool_rejected(self) -> None:
        with pytest.raises(GraphicalTargetError, match="k must be an integer"):
            _get_int({"k": True}, "k", 42)

    def test_get_int_int_value(self) -> None:
        assert _get_int({"k": 7}, "k", 42) == 7

    def test_get_int_numeric_string(self) -> None:
        assert _get_int({"k": " 9 "}, "k", 42) == 9

    def test_get_int_bad_string(self) -> None:
        with pytest.raises(GraphicalTargetError, match="k must be an integer"):
            _get_int({"k": "x"}, "k", 42)

    def test_get_int_float_rejected(self) -> None:
        with pytest.raises(GraphicalTargetError, match="k must be an integer"):
            _get_int({"k": 1.5}, "k", 42)


def test_principal_missing_raises_500() -> None:
    from types import SimpleNamespace

    from fastapi import HTTPException

    req = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(HTTPException) as exc:
        _principal(req)  # type: ignore[arg-type]
    assert exc.value.status_code == 500


class TestClampDimension:
    @pytest.mark.parametrize(
        ("value", "default", "expected"),
        [(0, 640, 640), (-5, 480, 480), (9000, 640, 8192), (800, 640, 800)],
    )
    def test_clamp(self, value: int, default: int, expected: int) -> None:
        assert _clamp_dimension(value, default) == expected
