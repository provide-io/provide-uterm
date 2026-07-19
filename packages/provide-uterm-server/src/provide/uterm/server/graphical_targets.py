#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Graphical-target model + tenant-scoped registry.

Python reference port of the C# canonical
(``packages/provide-uterm-csharp/src/Provide.Uterm/Server/GraphicalTargets.cs``)
and the Go port (``packages/provide-uterm-go/graphical/``).

A :class:`GraphicalTargetDefinition` describes a remote graphical console
(``memory`` or ``rfb``). Definitions live in a tenant-scoped
:class:`InMemoryGraphicalTargetRegistry`: every read/write is gated by a
:class:`GraphicalTargetScope` derived from the authenticated principal's tenant,
NEVER from client input. :meth:`GraphicalTargetDefinition.public_copy` strips
secrets from any value crossing the REST boundary. The wire shape (snake_case
JSON keys, error codes, validation and endpoint-parsing rules) mirrors the C#
canonical.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum, auto
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Callable

# --- Constants (GraphicalTargetConstants) ----------------------------------

PROTOCOL_MEMORY = "memory"
PROTOCOL_RFB = "rfb"
PROTOCOL_LITEVIRT = "litevirt"
SUPPORTED_PROTOCOLS = frozenset({PROTOCOL_MEMORY, PROTOCOL_RFB, PROTOCOL_LITEVIRT})

# Error code strings surfaced in the REST ``{"detail":{"code":...}}`` envelope.
ERR_INVALID_PAYLOAD = "graphical_target_invalid"
ERR_ALREADY_EXISTS = "graphical_target_exists"
ERR_NOT_FOUND = "graphical_target_not_found"
ERR_IMMUTABLE = "graphical_target_immutable"
ERR_CONFLICT = "graphical_target_conflict"
ERR_UNAVAILABLE = "graphical_target_unavailable"
ERR_BACKEND = "graphical_target_backend_error"
ERR_TENANT_MANAGED = "tenant_managed"
ERR_TARGET_ID_MISMATCH = "target_id_mismatch"

# GraphicalNamePattern (also the tenant name pattern) + SecretRefPattern. Shared
# verbatim with the C#/Go ports and ``server.api_keys`` tenant slug.
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SECRET_REF_PATTERN = re.compile(r"^(?:env:[A-Za-z_][A-Za-z0-9_]*|file:/[^\x00]+)$")

# The accepted create/update body keys (GraphicalTargetPayloadKeys).
PAYLOAD_KEYS = frozenset(
    {
        "tenant_id",
        "target_id",
        "display_name",
        "protocol",
        "endpoint",
        "secret",
        "width",
        "height",
        "ca_secret_ref",
        "client_cert_secret_ref",
        "client_key_secret_ref",
        "is_system",
        "is_static",
        "config",
    }
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GraphicalTargetErrorCode(Enum):
    """Coded registry/validation error (GraphicalTargetErrorCode)."""

    ALREADY_EXISTS = auto()
    NOT_FOUND = auto()
    IMMUTABLE = auto()
    FORBIDDEN = auto()
    CONFLICT = auto()
    INVALID = auto()
    CLOSED = auto()
    BACKEND = auto()


class GraphicalTargetError(Exception):
    """A coded registry/validation error (GraphicalTargetException)."""

    def __init__(self, code: GraphicalTargetErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --- Model (GraphicalTargetDefinition) --------------------------------------


@dataclass
class GraphicalTargetDefinition:
    """A single graphical-console definition (GraphicalTargetDefinition)."""

    target_id: str = ""
    tenant_id: str = ""
    display_name: str = ""
    protocol: str = PROTOCOL_RFB
    endpoint: str | None = None
    secret: str | None = None
    width: int = 640
    height: int = 480
    is_system: bool = False
    is_static: bool = False
    ca_secret_ref: str | None = None
    client_cert_secret_ref: str | None = None
    client_key_secret_ref: str | None = None
    created_by: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_by: str | None = None
    updated_at: datetime | None = None
    # Generic per-target, protocol-specific parameters (e.g. the litevirt
    # ``vm_name``). NOT a secret, so it survives ``public_copy``.
    config: dict[str, object] = field(default_factory=dict)

    def clone(self) -> GraphicalTargetDefinition:
        """Return a deep copy; the ``config`` map is copied so it never aliases."""
        return replace(self, config=dict(self.config))

    def public_copy(self) -> GraphicalTargetDefinition:
        """Return a clone with every secret stripped for the REST boundary."""
        copy = self.clone()
        copy.secret = None
        copy.ca_secret_ref = None
        copy.client_cert_secret_ref = None
        copy.client_key_secret_ref = None
        return copy

    def to_wire_dict(self) -> dict[str, object]:
        """Serialize to the snake_case wire shape (null optionals omitted)."""
        data: dict[str, object] = {
            "target_id": self.target_id,
            "tenant_id": self.tenant_id,
            "display_name": self.display_name,
            "protocol": self.protocol,
            "width": self.width,
            "height": self.height,
            "is_system": self.is_system,
            "is_static": self.is_static,
            "created_at": self.created_at.isoformat(),
        }
        if self.endpoint is not None:
            data["endpoint"] = self.endpoint
        if self.secret is not None:
            data["secret"] = self.secret
        if self.ca_secret_ref is not None:
            data["ca_secret_ref"] = self.ca_secret_ref
        if self.client_cert_secret_ref is not None:
            data["client_cert_secret_ref"] = self.client_cert_secret_ref
        if self.client_key_secret_ref is not None:
            data["client_key_secret_ref"] = self.client_key_secret_ref
        if self.created_by is not None:
            data["created_by"] = self.created_by
        if self.updated_by is not None:
            data["updated_by"] = self.updated_by
        if self.updated_at is not None:
            data["updated_at"] = self.updated_at.isoformat()
        if self.config:
            data["config"] = dict(self.config)
        return data

    def validate(self) -> None:
        """Normalize protocol/endpoint in place; raise on any violation."""
        if not _NAME_PATTERN.match(self.target_id):
            raise GraphicalTargetError(GraphicalTargetErrorCode.INVALID, "target_id must be a safe identifier")

        protocol = self.protocol.strip().lower()
        if protocol not in SUPPORTED_PROTOCOLS:
            raise GraphicalTargetError(GraphicalTargetErrorCode.INVALID, "unsupported protocol")
        self.protocol = protocol

        if protocol == PROTOCOL_RFB:
            host, port = parse_rfb_endpoint(self.endpoint)
            self.endpoint = f"{host}:{port}"
        elif protocol == PROTOCOL_LITEVIRT:
            host, port = parse_litevirt_endpoint(self.endpoint)
            self.endpoint = f"{host}:{port}"

        if self.width < 1 or self.width > 8192:
            raise GraphicalTargetError(GraphicalTargetErrorCode.INVALID, "width out of range")
        if self.height < 1 or self.height > 8192:
            raise GraphicalTargetError(GraphicalTargetErrorCode.INVALID, "height out of range")

        if self.tenant_id.strip() and not _NAME_PATTERN.match(self.tenant_id):
            raise GraphicalTargetError(GraphicalTargetErrorCode.INVALID, "tenant_id is invalid")

        for ref in (self.ca_secret_ref, self.client_cert_secret_ref, self.client_key_secret_ref):
            if ref is not None and not _SECRET_REF_PATTERN.match(ref):
                raise GraphicalTargetError(GraphicalTargetErrorCode.INVALID, "invalid secret reference syntax")


def parse_rfb_endpoint(raw_endpoint: str | None) -> tuple[str, int]:
    """Accept ``host:port`` / ``rfb://host:port`` / a ``dns:///`` prefix.

    Returns ``(host, port)``; requires a ``1..65535`` port. Ports the C#
    ``GraphicalTargetParsing.ParseRfbEndpoint`` rules.
    """
    raw = raw_endpoint or ""
    if not raw.strip():
        raise GraphicalTargetError(GraphicalTargetErrorCode.INVALID, "endpoint is required for protocol rfb")

    endpoint = raw.strip()
    if endpoint.lower().startswith("dns:///"):
        endpoint = endpoint[len("dns:///") :]

    if not endpoint.lower().startswith("rfb://"):
        if ":" not in endpoint:
            raise GraphicalTargetError(
                GraphicalTargetErrorCode.INVALID, "invalid endpoint; expected host:port or rfb://host:port"
            )
        endpoint = "rfb://" + endpoint

    parsed = urlparse(endpoint)
    if not parsed.hostname:
        raise GraphicalTargetError(
            GraphicalTargetErrorCode.INVALID, "invalid endpoint; expected host:port or rfb://host:port"
        )

    try:
        port = parsed.port
    except ValueError:
        raise GraphicalTargetError(GraphicalTargetErrorCode.INVALID, "invalid endpoint port") from None
    if port is None or port < 1 or port > 65535:
        raise GraphicalTargetError(GraphicalTargetErrorCode.INVALID, "invalid endpoint port")

    return parsed.hostname, port


def parse_litevirt_endpoint(raw_endpoint: str | None) -> tuple[str, int]:
    """Parse a litevirt gRPC endpoint (a plain ``host:port``, no wire scheme).

    Unlike rfb, a litevirt endpoint carries no ``rfb://`` scheme; a ``dns:///``
    prefix is still stripped. Ports the C# ``GraphicalTargetParsing.
    ParseLitevirtEndpoint`` / Go ``ParseLitevirtEndpoint`` rules.
    """
    raw = raw_endpoint or ""
    if not raw.strip():
        raise GraphicalTargetError(GraphicalTargetErrorCode.INVALID, "endpoint is required for protocol litevirt")

    endpoint = raw.strip()
    if endpoint.lower().startswith("dns:///"):
        endpoint = endpoint[len("dns:///") :]

    # Wrap in a throwaway scheme purely to lean on urlparse's host:port parsing.
    parsed = urlparse("grpc://" + endpoint)
    if not parsed.hostname:
        raise GraphicalTargetError(GraphicalTargetErrorCode.INVALID, "invalid endpoint; expected host:port")

    try:
        port = parsed.port
    except ValueError:
        raise GraphicalTargetError(GraphicalTargetErrorCode.INVALID, "invalid endpoint port") from None
    if port is None or port < 1 or port > 65535:
        raise GraphicalTargetError(GraphicalTargetErrorCode.INVALID, "invalid endpoint port")

    return parsed.hostname, port


# --- Scope (GraphicalTargetScope) -------------------------------------------


@dataclass(frozen=True)
class GraphicalTargetScope:
    """Tenant-isolation capability derived from the authenticated principal.

    NEVER from client input. Either a single-tenant scope or the system scope.
    """

    tenant_id: str | None
    is_system: bool

    @property
    def is_valid(self) -> bool:
        """Exactly one of system / tenant (GraphicalTargetScope.IsValid)."""
        return self.is_system != (self.tenant_id is not None)

    def permits(self, tenant_id: str | None) -> bool:
        """System scope permits any target; a tenant scope only its own."""
        if not self.is_valid:
            return False
        if self.is_system:
            return True
        return tenant_id is not None and tenant_id == self.tenant_id


def scope_for_tenant(tenant_id: str) -> tuple[GraphicalTargetScope | None, bool]:
    """A non-empty tenant id yields a tenant scope; blank yields ``(None, False)``."""
    if not tenant_id.strip():
        return None, False
    return GraphicalTargetScope(tenant_id=tenant_id, is_system=False), True


def system_scope() -> GraphicalTargetScope:
    """The system scope, used for seeded/system targets (GraphicalTargetScope.System)."""
    return GraphicalTargetScope(tenant_id=None, is_system=True)


# --- Registry (InMemoryGraphicalTargetRegistry) -----------------------------


class InMemoryGraphicalTargetRegistry:
    """Thread-safe registry: immutable seeded static + mutable runtime targets."""

    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        self._lock = threading.Lock()
        self._static: dict[str, GraphicalTargetDefinition] = {}
        self._runtime: dict[str, GraphicalTargetDefinition] = {}
        self._closed = False
        self._now = now or _utcnow

    def close(self) -> None:
        """Mark closed; every subsequent op raises ``CLOSED``."""
        with self._lock:
            self._closed = True

    def _ensure_open(self, scope: GraphicalTargetScope) -> None:
        if self._closed:
            raise GraphicalTargetError(GraphicalTargetErrorCode.CLOSED, "graphical target registry is closed")
        if not scope.is_valid:
            raise GraphicalTargetError(GraphicalTargetErrorCode.FORBIDDEN, "graphical target tenant scope denied")

    def get(self, scope: GraphicalTargetScope, target_id: str) -> GraphicalTargetDefinition | None:
        """Return the tenant-permitted target (static wins), else ``None``."""
        with self._lock:
            self._ensure_open(scope)
            static_target = self._static.get(target_id)
            if static_target is not None and scope.permits(static_target.tenant_id):
                return static_target.clone()
            runtime_target = self._runtime.get(target_id)
            if runtime_target is not None and scope.permits(runtime_target.tenant_id):
                return runtime_target.clone()
            return None

    def list(self, scope: GraphicalTargetScope) -> list[GraphicalTargetDefinition]:
        """Runtime + static merged (static wins), tenant-filtered, sorted by id."""
        with self._lock:
            self._ensure_open(scope)
            merged: dict[str, GraphicalTargetDefinition] = {}
            for target_id, target in self._runtime.items():
                if scope.permits(target.tenant_id):
                    merged[target_id] = target.clone()
            for target_id, target in self._static.items():
                if scope.permits(target.tenant_id):
                    merged[target_id] = target.clone()
            return [merged[target_id].clone() for target_id in sorted(merged)]

    def create(self, scope: GraphicalTargetScope, target: GraphicalTargetDefinition) -> GraphicalTargetDefinition:
        """Insert a new runtime target (InMemoryGraphicalTargetRegistry.Create)."""
        with self._lock:
            self._ensure_open(scope)
            clone = target.clone()
            if not scope.permits(clone.tenant_id):
                raise GraphicalTargetError(GraphicalTargetErrorCode.FORBIDDEN, "graphical target tenant scope denied")
            clone.validate()
            if clone.target_id in self._static or clone.target_id in self._runtime:
                raise GraphicalTargetError(GraphicalTargetErrorCode.ALREADY_EXISTS, "graphical target already exists")
            clone.created_at = self._now()
            self._runtime[clone.target_id] = clone
            return clone.clone()

    def update(self, scope: GraphicalTargetScope, target: GraphicalTargetDefinition) -> GraphicalTargetDefinition:
        """Replace an existing runtime target (InMemoryGraphicalTargetRegistry.Update)."""
        with self._lock:
            self._ensure_open(scope)
            clone = target.clone()
            if not scope.permits(clone.tenant_id):
                raise GraphicalTargetError(GraphicalTargetErrorCode.FORBIDDEN, "graphical target tenant scope denied")
            clone.validate()
            if clone.target_id in self._static:
                raise GraphicalTargetError(GraphicalTargetErrorCode.IMMUTABLE, "static graphical target is immutable")
            current = self._runtime.get(clone.target_id)
            if current is None:
                raise GraphicalTargetError(GraphicalTargetErrorCode.NOT_FOUND, "graphical target not found")
            if not scope.permits(current.tenant_id):
                raise GraphicalTargetError(GraphicalTargetErrorCode.FORBIDDEN, "graphical target tenant scope denied")
            clone.created_at = current.created_at
            clone.created_by = current.created_by
            clone.updated_at = self._now()
            self._runtime[clone.target_id] = clone
            return clone.clone()

    def delete(self, scope: GraphicalTargetScope, target_id: str) -> None:
        """Remove a runtime target (InMemoryGraphicalTargetRegistry.Delete)."""
        with self._lock:
            self._ensure_open(scope)
            static_target = self._static.get(target_id)
            if static_target is not None:
                if not scope.permits(static_target.tenant_id):
                    raise GraphicalTargetError(
                        GraphicalTargetErrorCode.FORBIDDEN, "graphical target tenant scope denied"
                    )
                raise GraphicalTargetError(GraphicalTargetErrorCode.IMMUTABLE, "static graphical target is immutable")
            current = self._runtime.get(target_id)
            if current is None:
                raise GraphicalTargetError(GraphicalTargetErrorCode.NOT_FOUND, "graphical target not found")
            if not scope.permits(current.tenant_id):
                raise GraphicalTargetError(GraphicalTargetErrorCode.FORBIDDEN, "graphical target tenant scope denied")
            del self._runtime[target_id]

    def add_static(self, target: GraphicalTargetDefinition) -> None:
        """Seed an immutable system target (InMemoryGraphicalTargetRegistry.AddStatic)."""
        with self._lock:
            clone = target.clone()
            clone.validate()
            clone.is_system = True
            if clone.target_id in self._static:
                raise GraphicalTargetError(GraphicalTargetErrorCode.CONFLICT, "duplicate graphical target_id")
            self._static[clone.target_id] = clone
