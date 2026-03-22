#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
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


class UiConfig(ServerBaseModel):
    """UI mount paths for the server application."""

    app_path: str = "/app"
    assets_path: str = "/_terminal"
    xterm_cdn: str = XTERM_CDN_DEFAULT
    fitaddon_cdn: str = FITADDON_CDN_DEFAULT
    fonts_cdn: str = FONTS_CDN_DEFAULT

    @field_validator("app_path")
    @classmethod
    def _normalize_app_path(cls, value: str) -> str:
        return _clean_path(value, "/app")

    @field_validator("assets_path")
    @classmethod
    def _normalize_assets_path(cls, value: str) -> str:
        return _clean_path(value, "/_terminal")


class RecordingConfig(ServerBaseModel):
    """File-backed recording settings."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    enabled_by_default: bool = False
    directory: Path = Path(".uterm-recordings")
    max_bytes: int = 0  # 0 = unlimited
    control_channel_mode: Literal["exclude", "wire"] = "exclude"

    @field_validator("max_bytes")
    @classmethod
    def _validate_max_bytes(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"recording.max_bytes must be >= 0 (0 = unlimited), got: {value}")
        return value


class ServerBindConfig(ServerBaseModel):
    """Bind and public URL settings."""

    host: str = TerminalDefaults.SERVER_HOST
    port: int = TerminalDefaults.SERVER_PORT
    public_base_url: str = ""
    title: str = "provide-terminal-server"
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
        if connector_type not in KNOWN_CONNECTOR_TYPES:
            label = session_id or "<unknown>"
            raise ValueError(
                f"invalid connector_type for {label!r}: {connector_type!r} — "
                f"must be one of {sorted(KNOWN_CONNECTOR_TYPES)}"
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
    last_error: str | None = None


class ServerConfig(ServerBaseModel):
    """Top-level application config for the standalone server."""

    server: ServerBindConfig = Field(default_factory=ServerBindConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    sessions: list[SessionDefinition] = Field(default_factory=list)


ServerModel: TypeAlias = (
    AuthConfig | UiConfig | RecordingConfig | ServerBindConfig | SessionDefinition | SessionRuntimeStatus | ServerConfig
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
