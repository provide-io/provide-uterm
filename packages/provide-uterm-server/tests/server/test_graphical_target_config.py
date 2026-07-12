from __future__ import annotations

import pytest
from pydantic import ValidationError

from provide.uterm.server.config import config_from_mapping
from provide.uterm.server.config_schema_graphical import GraphicalConfig, GraphicalTargetDefinition


def valid_target(**overrides: object) -> dict[str, object]:
    target: dict[str, object] = {
        "target_id": "prod-vnc",
        "endpoint": "dns:///graphical.internal:443",
        "tls_mode": "mtls",
        "ca_secret_ref": "file:secrets/ca.pem",  # pragma: allowlist secret
        "client_cert_secret_ref": "env:VNC_CLIENT_CERT",  # pragma: allowlist secret
        "client_key_secret_ref": "file:secrets/client.key",  # pragma: allowlist secret
        "expected_server_name": "graphical.internal",
        "allowed_vm_patterns": ["prod-*", "shared-??"],
        "tenant_id": "tenant-a",
        "minimum_role": "operator",
        "allowed_cidrs": ["10.0.0.0/8", "2001:db8::/32"],
        "audit_labels": {"zone": "west", "service": "vnc"},
    }
    target.update(overrides)
    return target


def test_target_normalizes_immutable_collections_and_serializes_references_only() -> None:
    target = GraphicalTargetDefinition.model_validate(valid_target(connect_timeout_s=1.5))
    assert target.allowed_vm_patterns == ("prod-*", "shared-??")
    assert target.allowed_cidrs == ("10.0.0.0/8", "2001:db8::/32")
    assert target.audit_labels == (("service", "vnc"), ("zone", "west"))
    assert target.ca_secret_ref is not None
    assert target.ca_secret_ref.value == "file:secrets/ca.pem"
    dumped = target.model_dump(mode="json")
    assert dumped["ca_secret_ref"] == "file:secrets/ca.pem"  # pragma: allowlist secret
    with pytest.raises(ValidationError):
        target.target_id = "changed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_id", "bad id"),
        ("target_id", "../bad"),
        ("endpoint", "https://host:443"),
        ("endpoint", "dns:/host:443"),
        ("endpoint", "dns:///host"),
        ("endpoint", "dns:///host:443?override=evil"),
        ("endpoint", "dns:///user@host:443"),
        ("tls_mode", "optional"),
        ("allowed_vm_patterns", [""]),
        ("allowed_vm_patterns", ["../prod"]),
        ("minimum_role", "root user"),
        ("tenant_id", "bad tenant!"),
        ("connect_timeout_s", 0),
        ("handshake_timeout_s", -1),
        ("read_timeout_s", 0),
        ("write_timeout_s", 0),
        ("shutdown_timeout_s", 0),
        ("max_grpc_message_bytes", 0),
        ("max_framebuffer_width", 0),
        ("max_framebuffer_height", 0),
        ("max_rectangles", 0),
        ("max_clipboard_bytes", 0),
        ("max_pixel_allocation_bytes", 0),
        ("allowed_cidrs", ["10.0.0.1/banana"]),
        ("audit_labels", {"bad label": "x"}),
    ],
)
def test_target_rejects_invalid_fields(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        GraphicalTargetDefinition.model_validate(valid_target(**{field: value}))


@pytest.mark.parametrize(
    "overrides",
    [
        {"tls_mode": "disabled", "ca_secret_ref": "env:CA"},  # pragma: allowlist secret
        {"tls_mode": "disabled", "expected_server_name": "host"},
        {"tls_mode": "tls", "client_cert_secret_ref": "env:CERT"},  # pragma: allowlist secret
        {"tls_mode": "mtls", "client_cert_secret_ref": None},
        {"tls_mode": "mtls", "client_key_secret_ref": None},
        {"tls_mode": "tls", "client_key_secret_ref": "env:KEY"},  # pragma: allowlist secret
    ],
)
def test_target_rejects_invalid_tls_combinations(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GraphicalTargetDefinition.model_validate(valid_target(**overrides))


def test_graphical_config_rejects_duplicates_and_dynamic_production() -> None:
    target = valid_target()
    with pytest.raises(ValidationError, match="duplicate graphical target_id"):
        GraphicalConfig(targets=[target, target])
    with pytest.raises(ValueError, match="allow_dynamic_targets"):
        config_from_mapping({"environment": "production", "graphical": {"allow_dynamic_targets": True}})


def test_server_mapping_loads_static_targets_and_dev_dynamic_mode() -> None:
    config = config_from_mapping(
        {
            "environment": "dev",
            "graphical": {"allow_dynamic_targets": True, "dynamic_allowed_cidrs": ["127.0.0.0/8"]},
            "graphical_targets": [valid_target()],
        }
    )
    assert config.graphical.allow_dynamic_targets is True
    assert config.graphical.dynamic_allowed_cidrs == ("127.0.0.0/8",)
    assert config.graphical_targets[0].target_id == "prod-vnc"
