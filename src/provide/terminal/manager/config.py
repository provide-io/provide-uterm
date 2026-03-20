#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Pydantic configuration model for the swarm manager."""

from __future__ import annotations

from pydantic import BaseModel, Field

from provide.terminal.manager.constants import (
    HEALTH_CHECK_INTERVAL_S,
    HEARTBEAT_TIMEOUT_S,
    SAVE_INTERVAL_S,
    TIMESERIES_INTERVAL_S,
)


class ManagerConfig(BaseModel):
    """Configuration for a generic swarm manager instance."""

    title: str = "Swarm Manager"
    host: str = "127.0.0.1"
    port: int = 2272
    max_bots: int = 200
    log_level: str = "info"

    # File paths (relative or absolute).
    state_file: str = ""
    timeseries_dir: str = ""
    log_dir: str = ""

    # Timing
    health_check_interval_s: int = HEALTH_CHECK_INTERVAL_S
    heartbeat_timeout_s: float = HEARTBEAT_TIMEOUT_S
    save_interval_s: float = SAVE_INTERVAL_S
    timeseries_interval_s: int = TIMESERIES_INTERVAL_S

    # Auth
    auth_token_env_var: str = "UTERM_MANAGER_API_TOKEN"  # noqa: S105
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:2272"])

    # Dashboard
    dashboard_html: str = ""
    static_dir: str = ""

    # Worker env-var prefix forwarded to subprocesses.
    worker_env_prefix: str = "UTERM_"

    # Paths that never require auth.
    auth_public_paths: list[str] = Field(
        default_factory=lambda: ["/", "/dashboard", "/hijack", "/hijack/", "/hijack/hijack.html"]
    )
    auth_public_prefixes: list[str] = Field(default_factory=lambda: ["/static/", "/hijack/assets/"])
