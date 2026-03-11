#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Config loading for the standalone terminal server app."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from provide.terminal.server.connectors import KNOWN_CONNECTOR_TYPES
from provide.terminal.server.models import (
    AuthConfig,
    RecordingConfig,
    ServerBindConfig,
    ServerConfig,
    SessionDefinition,
    UiConfig,
)


def _clean_path(value: str, fallback: str) -> str:
    text = str(value or fallback).strip()
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/") or "/"


def default_server_config() -> ServerConfig:
    """Return a runnable default config with one auto-start shell session."""
    return ServerConfig(
        auth=AuthConfig(mode="dev"),
        sessions=[
            SessionDefinition(
                session_id="demo-session",
                display_name="Interactive Shell Session",
                connector_type="shell",
                input_mode="open",
                auto_start=True,
                tags=["shell", "reference"],
            )
        ],
    )


def load_server_config(path: str | Path | None = None) -> ServerConfig:
    """Load server config from TOML, or return the default config if *path* is omitted."""
    if path is None:
        return default_server_config()
    cfg_path = Path(path)
    data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    config = config_from_mapping(data)
    if not config.recording.directory.is_absolute():
        config.recording.directory = (cfg_path.parent / config.recording.directory).resolve()
    return config


def config_from_mapping(data: dict[str, Any]) -> ServerConfig:
    """Build a validated config object from a plain mapping."""
    base = default_server_config()

    server_data = dict(data.get("server") or {})
    auth_data = dict(data.get("auth") or {})
    ui_data = dict(data.get("ui") or {})
    recording_data = dict(data.get("recording") or {})
    sessions_data = list(data.get("sessions") or [])

    server = ServerBindConfig(
        host=str(server_data.get("host", base.server.host)),
        port=int(server_data.get("port", base.server.port)),
        public_base_url=str(server_data.get("public_base_url", base.server.public_base_url)),
        title=str(server_data.get("title", base.server.title)),
        allowed_origins=[str(v) for v in server_data.get("allowed_origins", base.server.allowed_origins)],
    )
    auth = AuthConfig(
        mode=str(auth_data.get("mode", base.auth.mode)),
        principal_header=str(auth_data.get("principal_header", base.auth.principal_header)),
        role_header=str(auth_data.get("role_header", base.auth.role_header)),
        principal_cookie=str(auth_data.get("principal_cookie", base.auth.principal_cookie)),
        surface_cookie=str(auth_data.get("surface_cookie", base.auth.surface_cookie)),
        token_cookie=str(auth_data.get("token_cookie", base.auth.token_cookie)),
        jwt_issuer=str(auth_data.get("jwt_issuer", base.auth.jwt_issuer)),
        jwt_audience=str(auth_data.get("jwt_audience", base.auth.jwt_audience)),
        jwt_jwks_url=(None if auth_data.get("jwt_jwks_url") is None else str(auth_data.get("jwt_jwks_url"))),
        jwt_public_key_pem=(
            None if auth_data.get("jwt_public_key_pem") is None else str(auth_data.get("jwt_public_key_pem"))
        ),
        jwt_algorithms=[str(v) for v in auth_data.get("jwt_algorithms", base.auth.jwt_algorithms)],
        clock_skew_seconds=int(auth_data.get("clock_skew_seconds", base.auth.clock_skew_seconds)),
        jwt_roles_claim=str(auth_data.get("jwt_roles_claim", base.auth.jwt_roles_claim)),
        jwt_scopes_claim=str(auth_data.get("jwt_scopes_claim", base.auth.jwt_scopes_claim)),
        worker_bearer_token=(
            None if auth_data.get("worker_bearer_token") is None else str(auth_data.get("worker_bearer_token"))
        ),
    )
    ui = UiConfig(
        app_path=_clean_path(str(ui_data.get("app_path", base.ui.app_path)), base.ui.app_path),
        assets_path=_clean_path(str(ui_data.get("assets_path", base.ui.assets_path)), base.ui.assets_path),
        xterm_cdn=str(ui_data.get("xterm_cdn", base.ui.xterm_cdn)),
        fitaddon_cdn=str(ui_data.get("fitaddon_cdn", base.ui.fitaddon_cdn)),
        fonts_cdn=str(ui_data.get("fonts_cdn", base.ui.fonts_cdn)),
    )
    max_bytes = int(recording_data.get("max_bytes", base.recording.max_bytes))
    if max_bytes < 0:
        raise ValueError(f"recording.max_bytes must be >= 0 (0 = unlimited), got: {max_bytes}")
    recording = RecordingConfig(
        enabled_by_default=bool(recording_data.get("enabled_by_default", base.recording.enabled_by_default)),
        directory=Path(recording_data.get("directory", base.recording.directory)),
        max_bytes=max_bytes,
    )

    sessions: list[SessionDefinition] = []
    for raw in sessions_data:
        if not isinstance(raw, dict):
            continue
        session_id = str(raw.get("session_id", "")).strip()
        if not session_id:
            raise ValueError("session_id is required for each [[sessions]] entry")
        if not re.match(r"^[\w\-]+$", session_id):
            raise ValueError(f"session_id must match ^[\\w\\-]+$, got: {session_id!r}")
        connector_type = str(raw.get("connector_type", "shell")).strip() or "shell"
        if connector_type not in KNOWN_CONNECTOR_TYPES:
            raise ValueError(
                f"invalid connector_type for {session_id!r}: {connector_type!r} — "
                f"must be one of {sorted(KNOWN_CONNECTOR_TYPES)}"
            )
        input_mode = str(raw.get("input_mode", "open")).strip() or "open"
        if input_mode not in {"hijack", "open"}:
            raise ValueError(f"invalid input_mode for {session_id}: {input_mode}")
        known_fields = {
            "session_id",
            "display_name",
            "connector_type",
            "input_mode",
            "auto_start",
            "tags",
            "recording_enabled",
            "owner",
            "visibility",
        }
        visibility = str(raw.get("visibility", "public")).strip() or "public"
        if visibility not in {"public", "operator", "private"}:
            raise ValueError(f"invalid visibility for {session_id}: {visibility!r}")
        connector_config = {k: v for k, v in raw.items() if k not in known_fields}
        sessions.append(
            SessionDefinition(
                session_id=session_id,
                display_name=str(raw.get("display_name", session_id)),
                connector_type=connector_type,
                connector_config=connector_config,
                input_mode=input_mode,  # type: ignore[arg-type]
                auto_start=bool(raw.get("auto_start", True)),
                tags=[str(v) for v in raw.get("tags", [])],
                recording_enabled=(
                    None if raw.get("recording_enabled") is None else bool(raw.get("recording_enabled"))
                ),
                owner=(None if raw.get("owner") is None else str(raw.get("owner"))),
                visibility=visibility,  # type: ignore[arg-type]
            )
        )

    if sessions:
        base.sessions = sessions
    base.server = server
    base.auth = auth
    base.ui = ui
    base.recording = recording
    return base
