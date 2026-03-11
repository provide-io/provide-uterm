#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Typed models for the hosted terminal server application."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeAlias

SessionLifecycle = Literal["stopped", "starting", "running", "error"]

# CDN URLs for xterm.js and fonts loaded into the operator dashboard HTML.
# These are fetched from third-party CDNs without Subresource Integrity (SRI)
# hashes.  Operators who require supply-chain isolation should override these
# via UIConfig.xterm_cdn / UIConfig.fonts_cdn to point to self-hosted copies,
# or add SRI attributes by customising the UI template.
XTERM_CDN_DEFAULT = "https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0"
FITADDON_CDN_DEFAULT = "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0"
FONTS_CDN_DEFAULT = "https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;700&display=swap"


@dataclass(slots=True)
class AuthConfig:
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
    jwt_algorithms: list[str] = field(default_factory=lambda: ["HS256"])
    clock_skew_seconds: int = 15
    jwt_roles_claim: str = "roles"
    jwt_scopes_claim: str = "scope"
    worker_bearer_token: str | None = None


@dataclass(slots=True)
class UiConfig:
    """UI mount paths for the server application."""

    app_path: str = "/app"
    assets_path: str = "/_terminal"
    xterm_cdn: str = XTERM_CDN_DEFAULT
    fitaddon_cdn: str = FITADDON_CDN_DEFAULT
    fonts_cdn: str = FONTS_CDN_DEFAULT


@dataclass(slots=True)
class RecordingConfig:
    """File-backed recording settings."""

    enabled_by_default: bool = False
    directory: Path = Path(".uterm-recordings")
    max_bytes: int = 0  # 0 = unlimited


@dataclass(slots=True)
class ServerBindConfig:
    """Bind and public URL settings."""

    host: str = "127.0.0.1"
    port: int = 8780
    public_base_url: str = "http://127.0.0.1:8780"
    title: str = "provide-terminal-server"
    allowed_origins: list[str] = field(default_factory=list)
    max_sessions: int | None = None


@dataclass(slots=True)
class SessionDefinition:
    """Config-backed definition for a named hosted terminal session."""

    session_id: str
    display_name: str
    connector_type: str
    connector_config: dict[str, Any] = field(default_factory=dict)
    input_mode: Literal["hijack", "open"] = "open"
    auto_start: bool = True
    tags: list[str] = field(default_factory=list)
    recording_enabled: bool | None = None
    created_at: float = field(default_factory=time.time)
    owner: str | None = None
    visibility: Literal["public", "operator", "private"] = "public"
    ephemeral: bool = False


@dataclass(slots=True)
class SessionRuntimeStatus:
    """Runtime-facing summary for a hosted session."""

    session_id: str
    display_name: str
    connector_type: str
    lifecycle_state: SessionLifecycle
    input_mode: str
    connected: bool
    auto_start: bool
    tags: list[str]
    recording_enabled: bool
    recording_available: bool = False
    owner: str | None = None
    visibility: str = "public"
    last_error: str | None = None


@dataclass(slots=True)
class ServerConfig:
    """Top-level application config for the standalone server."""

    server: ServerBindConfig = field(default_factory=ServerBindConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    sessions: list[SessionDefinition] = field(default_factory=list)


ServerModel: TypeAlias = (
    AuthConfig | UiConfig | RecordingConfig | ServerBindConfig | SessionDefinition | SessionRuntimeStatus | ServerConfig
)


def model_dump(obj: ServerModel) -> dict[str, Any]:
    """Serialize a dataclass model to a plain dict."""
    return asdict(obj)
