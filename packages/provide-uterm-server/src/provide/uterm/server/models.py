#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Typed models for the hosted terminal server application."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from typing import Any, TypeAlias

from pydantic import ValidationError

from provide.uterm.bridge.contracts import InputMode, SessionLifecycle, Visibility
from provide.uterm.server.config_schema import (
    FITADDON_CDN_DEFAULT,
    FONTS_CDN_DEFAULT,
    XTERM_CDN_DEFAULT,
    AuditConfig,
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
    WebhooksConfig,
)

# Alias for backward compatibility if needed, though we should transition to UtermServerConfig.
ServerConfig = UtermServerConfig

# Explicit re-export list for mypy strict mode. Anything listed here is
# importable as ``from provide.uterm.server.models import X`` without
# triggering an ``[attr-defined]`` error in callers compiled under
# ``--strict``. The CDN-default constants are re-exported for the unified
# ``uterm`` CLI's ``proxy`` subcommand; the config classes are re-exported
# so callers don't need to know whether the type lives in this module or
# config_schema.
__all__ = [
    "FITADDON_CDN_DEFAULT",
    "FONTS_CDN_DEFAULT",
    "XTERM_CDN_DEFAULT",
    "AuditConfig",
    "AuthConfig",
    "ControlPlaneConfig",
    "GovernanceConfig",
    "InputMode",
    "PamConfig",
    "ProfileStoreConfig",
    "RecordingConfig",
    "SecurityConfig",
    "ServerBaseModel",
    "ServerBindConfig",
    "ServerConfig",
    "SessionDefinition",
    "SessionLifecycle",
    "TunnelConfig",
    "UiConfig",
    "UtermServerConfig",
    "ValidationError",
    "Visibility",
    "WebhooksConfig",
]


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
    | WebhooksConfig
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
