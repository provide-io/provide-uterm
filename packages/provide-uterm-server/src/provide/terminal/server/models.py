#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Typed models for the hosted terminal server application."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from typing import Any, TypeAlias

from pydantic import ValidationError  # noqa: TC002

from provide.terminal.bridge.contracts import InputMode, SessionLifecycle, Visibility  # noqa: TC001
from provide.terminal.server.config_schema import (
    FITADDON_CDN_DEFAULT,  # noqa: F401  re-exported for cli/__init__.py
    FONTS_CDN_DEFAULT,  # noqa: F401  re-exported for cli/__init__.py
    XTERM_CDN_DEFAULT,  # noqa: F401  re-exported for cli/__init__.py
    AuthConfig,
    ControlPlaneConfig,
    GovernanceConfig,
    PamConfig,
    ProfileStoreConfig,
    RecordingConfig,
    SecurityConfig,
    ServerBaseModel,
    ServerBindConfig,
    SessionDefinition,
    TunnelConfig,
    UiConfig,
    UtermServerConfig,
)

# Alias for backward compatibility if needed, though we should transition to UtermServerConfig.
ServerConfig = UtermServerConfig


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
    | UtermServerConfig
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
