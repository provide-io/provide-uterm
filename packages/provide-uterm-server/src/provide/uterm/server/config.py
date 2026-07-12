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

from provide.uterm.server.config_schema import UtermServerConfig
from provide.uterm.server.models import validation_error_message
from provide.uterm.server.secrets import SecretReference

_TABLE_SECTIONS = frozenset(
    {
        "server",
        "auth",
        "ui",
        "recording",
        "profiles",
        "security",
        "tunnel",
        "webhooks",
        "pam",
        "control_plane",
        "graphical",
    }
)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def default_server_config() -> UtermServerConfig:
    """Return a runnable default config."""
    return UtermServerConfig()


def config_from_mapping(data: dict[str, Any], *, config_dir: Path | None = None) -> UtermServerConfig:
    """Build a validated config object from a plain mapping."""
    normalized = dict(data)
    for section in _TABLE_SECTIONS:
        if section in normalized and not isinstance(normalized[section], dict):
            actual_type = type(normalized[section]).__name__
            raise ValueError(f"[{section}] must be a table (got {actual_type})")
    sessions = normalized.get("sessions")
    if isinstance(sessions, list):
        normalized["sessions"] = [entry for entry in sessions if isinstance(entry, dict)]
    targets = normalized.get("graphical_targets")
    if isinstance(targets, list) and config_dir is not None:
        secret_fields = ("ca_secret_ref", "client_cert_secret_ref", "client_key_secret_ref")
        normalized["graphical_targets"] = [
            {
                **entry,
                **{
                    field: SecretReference.parse(entry[field], base_dir=config_dir)
                    for field in secret_fields
                    if entry.get(field) is not None
                },
            }
            if isinstance(entry, dict)
            else entry
            for entry in targets
        ]
    try:
        merged = _deep_merge(default_server_config().model_dump(mode="python"), normalized)
        return UtermServerConfig.model_validate(merged)
    except ValidationError as exc:
        raise ValueError(validation_error_message(exc)) from exc


def load_server_config(path: str | Path | None = None) -> UtermServerConfig:
    """Load server config from TOML, or return the default config if *path* is omitted."""
    if path is None:
        return default_server_config()
    cfg_path = Path(path)
    data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    config = config_from_mapping(data, config_dir=cfg_path.parent)
    if not config.recording.directory.is_absolute():
        config.recording.directory = (cfg_path.parent / config.recording.directory).resolve()
    return config
