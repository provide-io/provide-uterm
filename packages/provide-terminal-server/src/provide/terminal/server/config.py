#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Config loading for the standalone terminal server app."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from provide.terminal.server.models import AuthConfig, ServerConfig, SessionDefinition, validation_error_message


def default_server_config() -> ServerConfig:
    """Return a runnable default config with one auto-start shell session."""
    return ServerConfig(
        auth=AuthConfig(mode="dev"),
        sessions=[
            SessionDefinition(
                session_id="provide-shell",
                display_name="Provide Shell",
                connector_type="shell",
                input_mode="open",
                auto_start=True,
                tags=["shell", "reference"],
            )
        ],
    )


def _merged_config_mapping(data: dict[str, Any]) -> dict[str, Any]:
    unknown_sections = set(data) - set(ServerConfig.model_fields)
    if unknown_sections:
        raise ValueError(f"Extra inputs are not permitted: {sorted(unknown_sections)!r}")
    base = default_server_config().model_dump(mode="python")
    merged = dict(base)
    for section in (
        "server",
        "auth",
        "control_plane",
        "ui",
        "recording",
        "profiles",
        "security",
        "tunnel",
        "pam",
        "governance",
    ):
        if section in data:
            if not isinstance(data[section], dict):
                raise ValueError(f"[{section}] must be a table, got {type(data[section]).__name__!r}")
            merged[section] = {**merged[section], **data[section]}
    for field in ("session_idle_timeout_s", "session_retention_s", "browser_rate_limit_per_sec"):
        if field in data:
            merged[field] = data[field]
    if "sessions" in data:
        merged["sessions"] = [entry for entry in data["sessions"] if isinstance(entry, dict)]
    return merged


def config_from_mapping(data: dict[str, Any]) -> ServerConfig:
    """Build a validated config object from a plain mapping."""
    try:
        return ServerConfig.model_validate(_merged_config_mapping(data))
    except ValidationError as exc:
        raise ValueError(validation_error_message(exc)) from exc


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
