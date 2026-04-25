#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Typed models for the hosted terminal server application."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from provide.terminal.defaults import TerminalDefaults
from provide.terminal.server.connectors import KNOWN_CONNECTOR_TYPES

SessionLifecycle = Literal["stopped", "starting", "running", "error"]
InputMode = Literal["hijack", "open"]
Visibility = Literal["public", "operator", "private"]

# CDN URLs for xterm.js and fonts loaded into the operator dashboard HTML.
# These are fetched from third-party CDNs without Subresource Integrity (SRI)
# hashes.  Operators who require supply-chain isolation should override these
# via UIConfig.xterm_cdn / UIConfig.fonts_cdn to point to self-hosted copies,
# or add SRI attributes by customising the UI template.
XTERM_CDN_DEFAULT = "https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0"
FITADDON_CDN_DEFAULT = "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0"
FONTS_CDN_DEFAULT = "https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;700&display=swap"


def _clean_path(value: str, fallback: str) -> str:
    text = str(value or fallback).strip()
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/") or "/"


class ServerBaseModel(BaseModel):
    """Base class for mutable server models."""

    model_config = ConfigDict(extra="forbid")


class AuthConfig(ServerBaseModel):
    """Authentication and principal-bridging settings for the server app."""

    mode: str = "jwt"
    principal_header: str = "x-uterm-principal"
    role_header: str = "x-uterm-role"
    principal_cookie: str = "uterm_principal"
    surface_cookie: str = "uterm_surface"
    token_cookie: str = "uterm_token"  # noqa: S105
    jwt_issuer: str = "provide-terminal"
    jwt_audience: str = "provide-terminal-server"
    jwt_jwks_url: str | None = None
    jwt_public_key_pem: str | None = None
    jwt_algorithms: list[str] = Field(default_factory=lambda: ["HS256"])
    clock_skew_seconds: int = 15
    jwt_roles_claim: str = "roles"
    jwt_scopes_claim: str = "scope"
    worker_bearer_token: str | None = None
    api_keys_enabled: bool = False  # Opt-in API key authentication
    header_mode_acknowledged: bool = False  # Must be set True to allow auth.mode='header'

    identity_provider: Literal["local", "webhook"] = "local"
    delegate_roles: bool = True
    webhook_idp_url: str | None = None
    webhook_idp_secret: str | None = None
    webhook_idp_timeout_s: float = 2.0


class UiConfig(ServerBaseModel):
    """UI mount paths for the server application."""

    app_path: str = "/app"
    assets_path: str = "/_terminal"
    xterm_cdn: str = XTERM_CDN_DEFAULT
    fitaddon_cdn: str = FITADDON_CDN_DEFAULT
    fonts_cdn: str = FONTS_CDN_DEFAULT
    # Subresource Integrity (SRI) hashes for the CDN scripts.  When set, the
    # <script> tags are emitted with integrity= and crossorigin=anonymous so
    # a compromised CDN swap would make the browser refuse to execute the
    # asset.  Hashes must match the exact version pinned in xterm_cdn /
    # fitaddon_cdn above.  Empty = no integrity check (default, but strongly
    # discouraged for production tunnel deployments).
    xterm_cdn_integrity: str = ""
    fitaddon_cdn_integrity: str = ""

    @field_validator("app_path")
    @classmethod
    def _normalize_app_path(cls, value: str) -> str:
        return _clean_path(value, "/app")

    @field_validator("assets_path")
    @classmethod
    def _normalize_assets_path(cls, value: str) -> str:
        return _clean_path(value, "/_terminal")


class RecordingConfig(ServerBaseModel):
    """Session recording settings."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    enabled_by_default: bool = False
    directory: Path = Path(".uterm-recordings")
    max_bytes: int = 0  # 0 = unlimited
    control_channel_mode: Literal["exclude", "wire"] = "exclude"
    
    store_type: Literal["local", "webhook"] = "local"
    webhook_url: str | None = None
    webhook_secret: str | None = None
    webhook_timeout_s: float = 2.0
    flush_interval_s: float = 5.0
    flush_batch_size: int = 100

    @field_validator("max_bytes")
    @classmethod
    def _validate_max_bytes(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"recording.max_bytes must be >= 0 (0 = unlimited), got: {value}")
        return value


class ControlPlaneConfig(ServerBaseModel):
    """Backend selection for the hosted server control plane."""

    backend: Literal["memory", "sqlite"] = "memory"
    database_url: str | None = None

    @model_validator(mode="after")
    def _validate_database_url(self) -> ControlPlaneConfig:
        if self.backend == "sqlite" and not str(self.database_url or "").strip():
            raise ValueError("control_plane.database_url is required when control_plane.backend='sqlite'")
        return self


class SecurityConfig(ServerBaseModel):
    """Configurable security response headers."""

    mode: Literal["strict", "dev"] = "strict"
    csp: str | None = None
    hsts: str | None = None
    x_frame_options: str | None = None
    x_content_type_options: str | None = None
    referrer_policy: str | None = None
    permissions_policy: str | None = None


class TunnelConfig(ServerBaseModel):
    """Tunnel sharing security settings."""

    token_ttl_s: int = 3600
    # Tunnel token transport. The HttpOnly ``uterm_tunnel_{id}`` cookie is the
    # primary transport and is always read by the auth middleware (set by the
    # page handler on initial load).  This knob controls whether a ``?token=``
    # QUERY STRING is *also* accepted alongside the cookie:
    #   "cookie" — cookie only; reject query-param tokens
    #   "both"   — accept cookie or query (recommended)
    #   "query"  — legacy alias for "both"; retained for config backward-compat
    token_transport: Literal["query", "cookie", "both"] = "both"  # noqa: S105
    cookie_secure: bool = True
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    ip_binding: bool = False

    @field_validator("token_ttl_s")
    @classmethod
    def _validate_ttl(cls, value: int) -> int:
        if value < 60:
            raise ValueError(f"tunnel.token_ttl_s must be >= 60, got: {value}")
        return value


class ProfileStoreConfig(ServerBaseModel):
    """File-backed profile store settings."""

    directory: Path = Path(".uterm-profiles")


class ServerBindConfig(ServerBaseModel):
    """Bind and public URL settings."""

    host: str = TerminalDefaults.SERVER_HOST
    port: int = TerminalDefaults.SERVER_PORT
    public_base_url: str = ""
    title: str = "provide-terminal-server"
    node_id: str = "default"
    allowed_origins: list[str] = Field(default_factory=list)
    max_sessions: int | None = None

    @model_validator(mode="after")
    def _derive_public_base_url(self) -> ServerBindConfig:
        if not self.public_base_url:
            self.public_base_url = f"http://{self.host}:{self.port}"
        return self


class SessionDefinition(ServerBaseModel):
    """Config-backed definition for a named hosted terminal session."""

    session_id: str
    display_name: str = ""
    connector_type: str = "shell"
    connector_config: dict[str, Any] = Field(default_factory=dict)
    input_mode: InputMode = "open"
    auto_start: bool = True
    tags: list[str] = Field(default_factory=list)
    recording_enabled: bool | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    owner: str | None = None
    visibility: Visibility = "public"
    ephemeral: bool = False
    presence: bool = False
    auto_transfer_idle_s: int = 30
    keystroke_queue: Literal["display", "replay"] = "display"

    @model_validator(mode="before")
    @classmethod
    def _collect_connector_config(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "display_name" not in data or data.get("display_name") is None:
            session_id = str(data.get("session_id", "")).strip()
            if session_id:
                data["display_name"] = session_id
        known_fields = set(cls.model_fields)
        connector_config = dict(data.get("connector_config", {}))
        for key in list(data):
            if key in known_fields:
                continue
            connector_config[key] = data.pop(key)
        data["connector_config"] = connector_config
        return data

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        session_id = value.strip()
        if not session_id:
            raise ValueError("session_id is required for each [[sessions]] entry")
        if not re.match(r"^[\w\-]+$", session_id):
            raise ValueError(f"session_id must match ^[\\w\\-]+$, got: {session_id!r}")
        return session_id

    @field_validator("connector_type")
    @classmethod
    def _validate_connector_type(cls, value: str, info: Any) -> str:
        connector_type = value.strip() or "shell"
        session_id = ""
        if isinstance(info.data, dict):
            session_id = str(info.data.get("session_id", "")).strip()

        # Only validate against the registry if it's already populated.
        # This prevents circular dependency issues during startup.
        from provide.terminal.server.connectors import registered_types
        known = registered_types()
        if known and connector_type not in known:
            label = session_id or "<unknown>"
            raise ValueError(
                f"invalid connector_type for {label!r}: {connector_type!r} — "
                f"must be one of {sorted(known)}"
            )
        return connector_type

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str, info: Any) -> str:
        if value == "":
            if isinstance(info.data, dict):
                return str(info.data.get("session_id", ""))
            return ""
        return str(value)

    @field_validator("input_mode", mode="before")
    @classmethod
    def _validate_input_mode(cls, value: Any, info: Any) -> Any:
        if value not in {"hijack", "open"}:
            session_id = ""
            if isinstance(info.data, dict):
                session_id = str(info.data.get("session_id", "")).strip()
            raise ValueError(f"invalid input_mode for {session_id or '<unknown>'}: {value}")
        return value

    @field_validator("visibility", mode="before")
    @classmethod
    def _validate_visibility(cls, value: Any, info: Any) -> Any:
        if value not in {"public", "operator", "private"}:
            session_id = ""
            if isinstance(info.data, dict):
                session_id = str(info.data.get("session_id", "")).strip()
            raise ValueError(f"invalid visibility for {session_id or '<unknown>'}: {value!r}")
        return value


class SessionRuntimeStatus(ServerBaseModel):
    """Runtime-facing summary for a hosted session."""

    session_id: str
    display_name: str
    created_at: datetime
    connector_type: str
    lifecycle_state: SessionLifecycle
    input_mode: InputMode
    connected: bool
    auto_start: bool
    tags: list[str]
    recording_enabled: bool
    recording_available: bool = False
    owner: str | None = None
    visibility: Visibility = "public"
    stopped_at: float | None = None
    last_error: str | None = None


class PamConfig(ServerBaseModel):
    """PAM session bridge settings (requires provide-terminal-platform)."""

    notify_socket: str | None = None
    mode: Literal["notify", "capture"] = "notify"
    auto_session: bool = False
    auto_session_command: str = "/bin/bash"
    relay_url: str | None = None  # Relay service base URL e.g. https://x.workers.dev
    relay_token: str | None = None  # Bearer token for relay service API


class GovernanceConfig(ServerBaseModel):
    """Configuration for external policy and telemetry hooks."""

    policy_webhook_url: str | None = None
    policy_webhook_secret: str | None = None
    policy_webhook_timeout_s: float = 2.0

    discovery_provider: str = "webhook"
    registry_webhook_url: str | None = None
    registry_webhook_secret: str | None = None
    registry_webhook_interval_s: float = 60.0

    authz_webhook_url: str | None = None
    authz_webhook_secret: str | None = None
    authz_webhook_timeout_s: float = 2.0

    # Behavioral Auditing
    behavioral_audit_url: str | None = None
    behavioral_audit_secret: str | None = None
    behavioral_audit_interval_s: float = 30.0
    behavioral_max_cps: float | None = None
    behavioral_min_jitter: float | None = None

    # Extensibility
    external_connectors: list[str] = Field(default_factory=list)


class ServerConfig(ServerBaseModel):
    """Top-level application config for the standalone server."""

    server: ServerBindConfig = Field(default_factory=ServerBindConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    control_plane: ControlPlaneConfig = Field(default_factory=ControlPlaneConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    profiles: ProfileStoreConfig = Field(default_factory=ProfileStoreConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    tunnel: TunnelConfig = Field(default_factory=TunnelConfig)
    pam: PamConfig = Field(default_factory=PamConfig)
    governance: GovernanceConfig = Field(default_factory=GovernanceConfig)
    sessions: list[SessionDefinition] = Field(default_factory=list)
    session_idle_timeout_s: int = 0  # 0 = disabled, >0 = seconds of inactivity before auto-cleanup
    session_retention_s: int = 0  # 0 = disabled, >0 = auto-delete stopped sessions older than N seconds
    browser_rate_limit_per_sec: float = 300  # WS messages/sec per browser connection (keystrokes)


ServerModel: TypeAlias = (
    AuthConfig
    | ControlPlaneConfig
    | UiConfig
    | RecordingConfig
    | ProfileStoreConfig
    | SecurityConfig
    | TunnelConfig
    | PamConfig
    | ServerBindConfig
    | SessionDefinition
    | SessionRuntimeStatus
    | ServerConfig
    | GovernanceConfig
)


def model_dump(obj: ServerModel) -> dict[str, Any]:
    """Serialize a server model to a plain dict."""
    return obj.model_dump(mode="python")


def validation_error_message(exc: ValidationError) -> str:
    """Return the first human-meaningful validation error message."""
    errors = exc.errors(include_url=False)
    if not errors:
        return str(exc)
    first = errors[0]
    return str(first.get("msg", exc))
