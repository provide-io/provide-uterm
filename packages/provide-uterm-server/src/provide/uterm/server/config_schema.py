#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Canonical configuration schema for the hosted terminal server application."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provide.uterm.bridge.contracts import InputMode, Visibility  # noqa: TC001
from provide.uterm.defaults import TerminalDefaults

# CDN URLs for xterm.js and fonts loaded into the operator dashboard HTML.
XTERM_CDN_DEFAULT = "https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0"
FITADDON_CDN_DEFAULT = "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0"
FONTS_CDN_DEFAULT = "https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;700&display=swap"
SERVER_BUILTIN_CONNECTOR_TYPES = frozenset({"shell", "ssh", "telnet", "websocket", "ushell"})


def _clean_path(value: str, fallback: str) -> str:
    text = str(value or fallback).strip()
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/") or "/"


class ServerBaseModel(BaseModel):
    """Base class for mutable server models with strict validation."""

    model_config = ConfigDict(extra="forbid")


class AuthConfig(ServerBaseModel):
    """Authentication and principal-bridging settings for the server app."""

    mode: str = "jwt"
    principal_header: str = "x-uterm-principal"
    role_header: str = "x-uterm-role"
    principal_cookie: str = "uterm_principal"
    role_cookie: str = "uterm_role"
    surface_cookie: str = "uterm_surface"
    token_cookie: str = "uterm_token"  # noqa: S105
    jwt_issuer: str = "provide-uterm"
    jwt_audience: str = "provide-uterm-server"
    jwt_jwks_url: str | None = None
    jwt_public_key_pem: str | None = None
    jwt_algorithms: list[str] = Field(default_factory=lambda: ["HS256"])
    clock_skew_seconds: int = 15
    jwt_roles_claim: str = "roles"
    jwt_scopes_claim: str = "scope"
    worker_bearer_token: str | None = None
    api_keys_enabled: bool = False
    header_mode_acknowledged: bool = False
    require_jwt_in_production: bool = False
    # Finding #4: when auth.mode='header', the server trusts X-Uterm-Role from
    # any caller.  When ``trusted_proxy_ips`` is non-empty, header-mode auth
    # is only honoured for connections whose source IP appears in the list;
    # other callers are downgraded to anonymous.  Required for non-loopback
    # binds (the startup validator rejects header mode on non-loopback hosts
    # unless this list is set).
    trusted_proxy_ips: list[str] = Field(default_factory=list)

    # Secret used to sign/verify identity control frames from upstream proxies.
    upstream_proxy_secret: str | None = None
    require_upstream_proxy_secret: bool = False

    identity_provider: Literal["local", "webhook"] = "local"
    delegate_roles: bool = True
    webhook_idp_url: str | None = None
    webhook_idp_secret: str | None = None
    webhook_idp_timeout_s: float = 2.0
    # Finding #7: webhook IdP failure mode.  Before this field, *any* exception
    # from the webhook (HTTP error, timeout, JSON parse failure, network down)
    # produced a synthetic ``viewer`` principal — the request silently passed
    # auth as an anonymous-equivalent viewer.  Default ``deny`` returns None
    # so the request fails authn (411).  ``viewer`` restores the old fail-open
    # behaviour for callers who explicitly want it.
    webhook_idp_on_failure: Literal["deny", "viewer"] = "deny"

    @model_validator(mode="after")
    def _validate_proxy_secret(self) -> AuthConfig:
        if self.require_upstream_proxy_secret and not str(self.upstream_proxy_secret or "").strip():
            raise ValueError("auth.upstream_proxy_secret is required when auth.require_upstream_proxy_secret=True")
        return self

    @model_validator(mode="after")
    def _validate_outbound_url_schemes(self) -> AuthConfig:
        _require_secure_url(self.webhook_idp_url, "auth.webhook_idp_url")
        _require_secure_url(self.jwt_jwks_url, "auth.jwt_jwks_url")
        return self


class UiConfig(ServerBaseModel):
    """UI mount paths for the server application."""

    app_path: str = "/app"
    assets_path: str = "/_terminal"
    xterm_cdn: str = XTERM_CDN_DEFAULT
    fitaddon_cdn: str = FITADDON_CDN_DEFAULT
    fonts_cdn: str = FONTS_CDN_DEFAULT
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
    retention_s: int = 0  # 0 = keep indefinitely
    control_channel_mode: Literal["exclude", "wire"] = "exclude"
    redact_sensitive: bool = True

    store_type: Literal["local", "memory", "null", "webhook"] = "local"
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

    @field_validator("retention_s")
    @classmethod
    def _validate_retention_s(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"recording.retention_s must be >= 0 (0 = keep indefinitely), got: {value}")
        return value

    @model_validator(mode="after")
    def _validate_outbound_url_schemes(self) -> RecordingConfig:
        _require_secure_url(self.webhook_url, "recording.webhook_url")
        return self


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
    token_transport: Literal["query", "cookie", "both"] = "cookie"  # noqa: S105
    cookie_secure: bool = True
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    ip_binding: bool = False

    @field_validator("token_ttl_s")
    @classmethod
    def _validate_ttl(cls, value: int) -> int:
        if value < 60:
            raise ValueError(f"tunnel.token_ttl_s must be >= 60, got: {value}")
        return value


class WebhooksConfig(ServerBaseModel):
    """Webhook delivery safety settings."""

    allow_loopback_destinations: bool = False


class ProfileStoreConfig(ServerBaseModel):
    """File-backed profile store settings."""

    directory: Path = Path(".uterm-profiles")


class ServerBindConfig(ServerBaseModel):
    """Bind and public URL settings."""

    host: str = TerminalDefaults.SERVER_HOST
    port: int = TerminalDefaults.SERVER_PORT
    public_base_url: str = ""
    title: str = "provide-uterm-server"
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
        try:
            from provide.uterm.server.connectors import registered_types

            known = registered_types()
            valid = known | SERVER_BUILTIN_CONNECTOR_TYPES
            if known and connector_type not in valid:
                label = session_id or "<unknown>"
                raise ValueError(
                    f"invalid connector_type for {label!r}: {connector_type!r} — must be one of {sorted(valid)}"
                )
        except ImportError:  # pragma: no cover — server.connectors is always present in this package
            # Fallback for environments where server.connectors isn't available
            pass
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


class PamConfig(ServerBaseModel):
    """PAM session bridge settings (requires provide-uterm-platform)."""

    notify_socket: str | None = None
    mode: Literal["notify", "capture"] = "notify"
    auto_session: bool = False
    auto_session_command: str = "/bin/bash"
    relay_url: str | None = None
    relay_token: str | None = None

    @model_validator(mode="after")
    def _validate_outbound_url_schemes(self) -> PamConfig:
        _require_secure_url(self.relay_url, "pam.relay_url")
        return self


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _require_secure_url(url: str | None, field_name: str) -> None:
    """Reject a cleartext ``http://`` outbound URL unless its host is loopback.

    ``https://`` is always allowed; ``http://`` is allowed only for loopback
    hosts (local dev). Any other scheme, or ``http://`` to a routable host,
    raises -- these channels carry HMAC secrets, auth headers, and the JWKS
    used to validate admin tokens, so cleartext to a remote host is unsafe.
    """
    if not url:
        return
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme != "http":
        raise ValueError(f"{field_name} must use http(s)")
    host = (parsed.hostname or "").lower()
    if host in _LOOPBACK_HOSTS or host.endswith(".localhost"):
        return
    raise ValueError(f"{field_name} must use https:// (cleartext http:// is only allowed for loopback hosts)")


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

    behavioral_audit_url: str | None = None
    behavioral_audit_secret: str | None = None
    behavioral_audit_interval_s: float = 30.0
    behavioral_max_cps: float | None = None
    behavioral_min_jitter: float | None = None

    external_connectors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_outbound_url_schemes(self) -> GovernanceConfig:
        _require_secure_url(self.policy_webhook_url, "governance.policy_webhook_url")
        _require_secure_url(self.registry_webhook_url, "governance.registry_webhook_url")
        _require_secure_url(self.authz_webhook_url, "governance.authz_webhook_url")
        _require_secure_url(self.behavioral_audit_url, "governance.behavioral_audit_url")
        return self


class UtermServerConfig(ServerBaseModel):
    """Top-level application config for the standalone server."""

    server: ServerBindConfig = Field(default_factory=ServerBindConfig)
    auth: AuthConfig = Field(default_factory=lambda: AuthConfig(mode="dev_token"))
    control_plane: ControlPlaneConfig = Field(default_factory=ControlPlaneConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    profiles: ProfileStoreConfig = Field(default_factory=ProfileStoreConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    tunnel: TunnelConfig = Field(default_factory=TunnelConfig)
    webhooks: WebhooksConfig = Field(default_factory=WebhooksConfig)
    pam: PamConfig = Field(default_factory=PamConfig)
    governance: GovernanceConfig = Field(default_factory=GovernanceConfig)
    sessions: list[SessionDefinition] = Field(
        default_factory=lambda: [
            SessionDefinition(
                session_id="provide-shell",
                display_name="Provide Shell",
                connector_type="shell",
                input_mode="open",
                auto_start=True,
                tags=["shell", "reference"],
            )
        ]
    )
    session_idle_timeout_s: int = 0
    session_retention_s: int = 0
    browser_rate_limit_per_sec: float = 300


SessionDefinition.model_rebuild()
UtermServerConfig.model_rebuild()
