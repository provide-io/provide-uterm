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

from provide.terminal.server.config_schema import UtermServerConfig
from provide.terminal.server.models import validation_error_message


def default_server_config() -> UtermServerConfig:
    """Return a runnable default config."""
    return UtermServerConfig()


def config_from_mapping(data: dict[str, Any]) -> UtermServerConfig:
    """Build a validated config object from a plain mapping."""
    try:
        return UtermServerConfig.model_validate(data)
    except ValidationError as exc:
        raise ValueError(validation_error_message(exc)) from exc


def load_server_config(path: str | Path | None = None) -> UtermServerConfig:
    """Load server config from TOML, or return the default config if *path* is omitted."""
    if path is None:
        return default_server_config()
    cfg_path = Path(path)
    data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    config = config_from_mapping(data)
    if not config.recording.directory.is_absolute():
        config.recording.directory = (cfg_path.parent / config.recording.directory).resolve()
    return config
