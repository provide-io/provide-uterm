#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for the graphical-target model + tenant-scoped registry.

Covers GraphicalTargetDefinition (validate / parse / public_copy / wire-dict),
GraphicalTargetScope, and InMemoryGraphicalTargetRegistry (tenant isolation,
static immutability, closed state, static+runtime merge).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from provide.uterm.server.graphical_targets import (
    PROTOCOL_LITEVIRT,
    PROTOCOL_MEMORY,
    PROTOCOL_RFB,
    GraphicalTargetDefinition,
    GraphicalTargetError,
    GraphicalTargetErrorCode,
    parse_litevirt_endpoint,
    parse_rfb_endpoint,
    scope_for_tenant,
    system_scope,
)


def _fixed_clock() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def _def(**kw: object) -> GraphicalTargetDefinition:
    base: dict[str, object] = {"target_id": "gt-1", "tenant_id": "acme", "protocol": "memory"}
    base.update(kw)
    return GraphicalTargetDefinition(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# parse_rfb_endpoint
# ---------------------------------------------------------------------------


class TestParseRfbEndpoint:
    def test_none_is_required(self) -> None:
        with pytest.raises(GraphicalTargetError) as exc:
            parse_rfb_endpoint(None)
        assert exc.value.code is GraphicalTargetErrorCode.INVALID
        assert "required" in exc.value.message

    def test_blank_is_required(self) -> None:
        with pytest.raises(GraphicalTargetError):
            parse_rfb_endpoint("   ")

    def test_host_port(self) -> None:
        assert parse_rfb_endpoint("vm.local:5900") == ("vm.local", 5900)

    def test_rfb_scheme(self) -> None:
        assert parse_rfb_endpoint("rfb://host.example:5901") == ("host.example", 5901)

    def test_rfb_scheme_case_insensitive(self) -> None:
        assert parse_rfb_endpoint("RFB://host.example:5901") == ("host.example", 5901)

    def test_dns_prefix_stripped(self) -> None:
        assert parse_rfb_endpoint("dns:///vm.local:5902") == ("vm.local", 5902)

    def test_missing_colon_rejected(self) -> None:
        with pytest.raises(GraphicalTargetError) as exc:
            parse_rfb_endpoint("hostonly")
        assert "expected host:port" in exc.value.message

    def test_empty_host_rejected(self) -> None:
        with pytest.raises(GraphicalTargetError) as exc:
            parse_rfb_endpoint("rfb://:5900")
        assert "expected host:port" in exc.value.message

    def test_port_out_of_range_high_raises_invalid_port(self) -> None:
        with pytest.raises(GraphicalTargetError) as exc:
            parse_rfb_endpoint("host:99999")
        assert exc.value.message == "invalid endpoint port"

    def test_missing_port_rejected(self) -> None:
        with pytest.raises(GraphicalTargetError) as exc:
            parse_rfb_endpoint("rfb://host")
        assert exc.value.message == "invalid endpoint port"

    def test_port_zero_rejected(self) -> None:
        with pytest.raises(GraphicalTargetError) as exc:
            parse_rfb_endpoint("host:0")
        assert exc.value.message == "invalid endpoint port"


# ---------------------------------------------------------------------------
# parse_litevirt_endpoint (plain host:port, no rfb:// scheme)
# ---------------------------------------------------------------------------


class TestParseLitevirtEndpoint:
    def test_none_is_required(self) -> None:
        with pytest.raises(GraphicalTargetError) as exc:
            parse_litevirt_endpoint(None)
        assert exc.value.code is GraphicalTargetErrorCode.INVALID
        assert "required" in exc.value.message

    def test_blank_is_required(self) -> None:
        with pytest.raises(GraphicalTargetError):
            parse_litevirt_endpoint("   ")

    def test_host_port(self) -> None:
        assert parse_litevirt_endpoint("vm.local:9000") == ("vm.local", 9000)

    def test_dns_prefix_stripped(self) -> None:
        assert parse_litevirt_endpoint("dns:///vm.local:9001") == ("vm.local", 9001)

    def test_empty_host_rejected(self) -> None:
        with pytest.raises(GraphicalTargetError) as exc:
            parse_litevirt_endpoint(":9000")
        assert "expected host:port" in exc.value.message

    def test_missing_port_rejected(self) -> None:
        with pytest.raises(GraphicalTargetError) as exc:
            parse_litevirt_endpoint("hostonly")
        assert exc.value.message == "invalid endpoint port"

    def test_port_out_of_range_high(self) -> None:
        with pytest.raises(GraphicalTargetError) as exc:
            parse_litevirt_endpoint("host:99999")
        assert exc.value.message == "invalid endpoint port"

    def test_port_zero_rejected(self) -> None:
        with pytest.raises(GraphicalTargetError) as exc:
            parse_litevirt_endpoint("host:0")
        assert exc.value.message == "invalid endpoint port"


# ---------------------------------------------------------------------------
# GraphicalTargetDefinition.validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_memory_ok(self) -> None:
        d = _def(protocol="memory", width=100, height=100)
        d.validate()
        assert d.protocol == "memory"
        assert d.endpoint is None

    def test_rfb_normalizes_endpoint(self) -> None:
        d = _def(protocol="RFB", endpoint="vm.local:5900")
        d.validate()
        assert d.protocol == "rfb"
        assert d.endpoint == "vm.local:5900"

    def test_litevirt_normalizes_endpoint(self) -> None:
        d = _def(protocol="LITEVIRT", endpoint="vm.local:9000")
        d.validate()
        assert d.protocol == "litevirt"
        assert d.endpoint == "vm.local:9000"

    def test_bad_target_id(self) -> None:
        d = _def(target_id="bad id!")
        with pytest.raises(GraphicalTargetError, match="safe identifier"):
            d.validate()

    def test_unsupported_protocol(self) -> None:
        d = _def(protocol="ftp")
        with pytest.raises(GraphicalTargetError, match="unsupported protocol"):
            d.validate()

    def test_width_too_small(self) -> None:
        d = _def(width=0)
        with pytest.raises(GraphicalTargetError, match="width out of range"):
            d.validate()

    def test_width_too_large(self) -> None:
        d = _def(width=8193)
        with pytest.raises(GraphicalTargetError, match="width out of range"):
            d.validate()

    def test_height_too_small(self) -> None:
        d = _def(height=0)
        with pytest.raises(GraphicalTargetError, match="height out of range"):
            d.validate()

    def test_height_too_large(self) -> None:
        d = _def(height=8193)
        with pytest.raises(GraphicalTargetError, match="height out of range"):
            d.validate()

    def test_blank_tenant_allowed(self) -> None:
        d = _def(tenant_id="  ", protocol="memory")
        d.validate()  # blank tenant skips the pattern check

    def test_invalid_tenant(self) -> None:
        d = _def(tenant_id="bad tenant!")
        with pytest.raises(GraphicalTargetError, match="tenant_id is invalid"):
            d.validate()

    def test_valid_secret_refs(self) -> None:
        d = _def(
            protocol="memory",
            ca_secret_ref="env:CA_BUNDLE",  # pragma: allowlist secret
            client_cert_secret_ref="file:/etc/cert.pem",  # pragma: allowlist secret
            client_key_secret_ref=None,
        )
        d.validate()

    def test_invalid_secret_ref(self) -> None:
        d = _def(protocol="memory", ca_secret_ref="not-a-ref")  # pragma: allowlist secret
        with pytest.raises(GraphicalTargetError, match="secret reference"):  # pragma: allowlist secret
            d.validate()


# ---------------------------------------------------------------------------
# public_copy / to_wire_dict / clone
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_public_copy_strips_secrets(self) -> None:
        d = _def(
            secret="sekret",  # pragma: allowlist secret
            ca_secret_ref="env:CA",  # pragma: allowlist secret
            client_cert_secret_ref="file:/c.pem",  # pragma: allowlist secret
            client_key_secret_ref="file:/k.pem",  # pragma: allowlist secret
        )
        pub = d.public_copy()
        assert pub.secret is None
        assert pub.ca_secret_ref is None
        assert pub.client_cert_secret_ref is None
        assert pub.client_key_secret_ref is None
        # original untouched
        assert d.secret == "sekret"  # pragma: allowlist secret

    def test_clone_is_independent(self) -> None:
        d = _def()
        c = d.clone()
        c.display_name = "changed"
        assert d.display_name != "changed"

    def test_clone_config_is_independent(self) -> None:
        d = _def(config={"vm_name": "web01"})
        c = d.clone()
        c.config["vm_name"] = "db01"
        assert d.config["vm_name"] == "web01"

    def test_public_copy_retains_config(self) -> None:
        # config is NOT a secret — it survives the REST boundary.
        d = _def(config={"vm_name": "web01"})
        assert d.public_copy().config == {"vm_name": "web01"}

    def test_wire_dict_omits_empty_config(self) -> None:
        assert "config" not in _def(protocol="memory").to_wire_dict()

    def test_wire_dict_includes_config(self) -> None:
        d = _def(protocol="litevirt", endpoint="vm:9000", config={"vm_name": "web01"})
        assert d.to_wire_dict()["config"] == {"vm_name": "web01"}

    def test_wire_dict_omits_none_optionals(self) -> None:
        d = _def(protocol="memory", endpoint=None)
        wire = d.to_wire_dict()
        for key in (
            "endpoint",
            "secret",
            "ca_secret_ref",
            "client_cert_secret_ref",
            "client_key_secret_ref",
            "created_by",
            "updated_by",
            "updated_at",
        ):
            assert key not in wire
        assert wire["target_id"] == "gt-1"
        assert wire["created_at"].endswith("+00:00")

    def test_wire_dict_includes_present_optionals(self) -> None:
        d = _def(
            protocol="rfb",
            endpoint="vm:5900",
            secret="s",  # pragma: allowlist secret
            ca_secret_ref="env:CA",  # pragma: allowlist secret
            client_cert_secret_ref="file:/c",  # pragma: allowlist secret
            client_key_secret_ref="file:/k",  # pragma: allowlist secret
            created_by="alice",
            updated_by="bob",
            updated_at=_fixed_clock(),
        )
        wire = d.to_wire_dict()
        assert wire["endpoint"] == "vm:5900"
        assert wire["secret"] == "s"  # pragma: allowlist secret
        assert wire["ca_secret_ref"] == "env:CA"  # pragma: allowlist secret
        assert wire["client_cert_secret_ref"] == "file:/c"  # pragma: allowlist secret
        assert wire["client_key_secret_ref"] == "file:/k"  # pragma: allowlist secret
        assert wire["created_by"] == "alice"
        assert wire["updated_by"] == "bob"
        assert wire["updated_at"] == _fixed_clock().isoformat()


# ---------------------------------------------------------------------------
# GraphicalTargetScope
# ---------------------------------------------------------------------------


class TestScope:
    def test_blank_tenant_no_scope(self) -> None:
        scope, ok = scope_for_tenant("  ")
        assert ok is False
        assert scope is None

    def test_tenant_scope(self) -> None:
        scope, ok = scope_for_tenant("acme")
        assert ok is True
        assert scope is not None
        assert scope.is_valid
        assert scope.permits("acme")
        assert not scope.permits("other")
        assert not scope.permits(None)

    def test_system_scope_permits_all(self) -> None:
        scope = system_scope()
        assert scope.is_valid
        assert scope.permits("acme")
        assert scope.permits(None)

    def test_invalid_scope_permits_nothing(self) -> None:
        from provide.uterm.server.graphical_targets import GraphicalTargetScope

        # Neither system nor tenant → not valid.
        bad = GraphicalTargetScope(tenant_id=None, is_system=False)
        assert not bad.is_valid
        assert not bad.permits("acme")


def test_protocol_constants() -> None:
    assert PROTOCOL_MEMORY == "memory"
    assert PROTOCOL_RFB == "rfb"
    assert PROTOCOL_LITEVIRT == "litevirt"
