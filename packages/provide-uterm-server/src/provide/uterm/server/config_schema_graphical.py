#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Strict static configuration for graphical connection targets."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from provide.uterm.server.secrets import SecretReference  # noqa: TC001 -- Pydantic needs runtime type

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_VM_PATTERN = re.compile(r"^[A-Za-z0-9_*?.:-]{1,256}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _positive(value: int | float) -> int | float:
    if value <= 0:
        raise ValueError("value must be positive")
    return value


PositiveFloat = Annotated[float, AfterValidator(_positive)]
PositiveInt = Annotated[int, AfterValidator(_positive)]


class GraphicalTargetDefinition(BaseModel):
    """Immutable target policy containing references, never resolved secrets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str
    endpoint: str
    tls_mode: Literal["disabled", "tls", "mtls"] = "tls"
    ca_secret_ref: SecretReference | None = None
    client_cert_secret_ref: SecretReference | None = None
    client_key_secret_ref: SecretReference | None = None
    expected_server_name: str | None = None
    allowed_vm_patterns: tuple[str, ...] = ("*",)
    tenant_id: str | None = None
    minimum_role: str = "viewer"
    connect_timeout_s: PositiveFloat = 10.0
    handshake_timeout_s: PositiveFloat = 10.0
    read_timeout_s: PositiveFloat = 30.0
    write_timeout_s: PositiveFloat = 30.0
    shutdown_timeout_s: PositiveFloat = 5.0
    max_grpc_message_bytes: PositiveInt = 16 * 1024 * 1024
    max_framebuffer_width: PositiveInt = 8192
    max_framebuffer_height: PositiveInt = 8192
    max_rectangles: PositiveInt = 4096
    max_clipboard_bytes: PositiveInt = 1024 * 1024
    max_pixel_allocation_bytes: PositiveInt = 256 * 1024 * 1024
    allowed_cidrs: tuple[str, ...] = ()
    audit_labels: tuple[tuple[str, str], ...] = ()

    @field_validator("target_id", "minimum_role")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("must be a safe identifier")
        return value

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant(cls, value: str | None) -> str | None:
        if value is not None and not _NAME.fullmatch(value):
            raise ValueError("tenant_id must be a safe identifier")
        return value

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if not value.startswith("dns:///") or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("endpoint must use dns:///host:port syntax")
        address = parsed.path[1:]
        host, separator, port = address.rpartition(":")
        if not separator or not host or not port.isdigit() or not 1 <= int(port) <= 65535 or "@" in host:
            raise ValueError("endpoint must include a valid host and port")
        return value

    @field_validator("allowed_vm_patterns")
    @classmethod
    def _validate_patterns(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or any(not _VM_PATTERN.fullmatch(value) for value in values):
            raise ValueError("allowed_vm_patterns must contain safe glob patterns")
        return tuple(dict.fromkeys(values))

    @field_validator("allowed_cidrs")
    @classmethod
    def _validate_cidrs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        try:
            return tuple(str(ipaddress.ip_network(value, strict=True)) for value in dict.fromkeys(values))
        except ValueError as exc:
            raise ValueError("allowed_cidrs must contain canonical networks") from exc

    @field_validator("audit_labels", mode="before")
    @classmethod
    def _normalize_labels(cls, value: Any) -> tuple[tuple[str, str], ...]:
        pairs = value.items() if isinstance(value, Mapping) else value
        normalized = tuple(sorted((str(key), str(item)) for key, item in pairs))
        if any(not _LABEL.fullmatch(key) or len(item) > 256 for key, item in normalized):
            raise ValueError("audit_labels contain an invalid label")
        return normalized

    @model_validator(mode="after")
    def _validate_tls(self) -> GraphicalTargetDefinition:
        client_pair = self.client_cert_secret_ref is not None and self.client_key_secret_ref is not None
        any_client = self.client_cert_secret_ref is not None or self.client_key_secret_ref is not None
        if self.tls_mode == "disabled" and (self.ca_secret_ref is not None or self.expected_server_name is not None):
            raise ValueError("disabled TLS may not specify CA or server name")
        if self.tls_mode != "mtls" and any_client:
            raise ValueError("client certificate references require mtls")
        if self.tls_mode == "mtls" and not client_pair:
            raise ValueError("mtls requires both client certificate and key references")
        return self


class GraphicalConfig(BaseModel):
    """Global graphical-target behavior."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    allow_dynamic_targets: bool = False
    dynamic_allowed_cidrs: tuple[str, ...] = ()
    targets: tuple[GraphicalTargetDefinition, ...] = Field(default=(), exclude=True)

    @model_validator(mode="after")
    def _unique_targets(self) -> GraphicalConfig:
        ids = [target.target_id for target in self.targets]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate graphical target_id")
        GraphicalTargetDefinition._validate_cidrs(self.dynamic_allowed_cidrs)
        return self
