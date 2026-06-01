#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Pydantic configuration model for the swarm manager."""

from __future__ import annotations

from pydantic import BaseModel, Field

from provide.uterm.manager.constants import (
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
    max_agents: int = 200
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
    # Optional low-privilege worker token: when set, authorizes ONLY the
    # worker-self-report routes (POST /agent/{id}/status + /register). Unset =>
    # those routes still require the operator token (backward compatible).
    auth_worker_token_env_var: str = "UTERM_MANAGER_WORKER_TOKEN"  # noqa: S105
    # When True, the manager rejects the raw fleet-shared worker token on the
    # self-report routes and accepts ONLY the per-agent token derived from the
    # worker secret (HMAC-SHA256 over the agent_id), blocking cross-agent
    # impersonation. When False (default) the raw fleet token is also accepted
    # for backward compatibility with un-migrated workers, but the derived
    # per-agent token remains bound to the request path either way. Production
    # deployments should enable this.
    enforce_per_agent_worker_token: bool = False
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:2272"])

    # Dashboard
    dashboard_html: str = ""
    static_dir: str = ""

    # Worker env-var prefix forwarded to subprocesses.
    worker_env_prefix: str = "UTERM_"
    # Optional worker process resource limits (0 = disabled).
    worker_rlimit_nofile_soft: int = 0
    worker_rlimit_nofile_hard: int = 0
    worker_rlimit_as_mb: int = 0
    worker_rlimit_cpu_s: int = 0

    # Spawn sandbox: directory that swarm config files must live under. When
    # empty, the UTERM_CONFIG_DIR env var is consulted; if neither is set, spawn
    # requests are refused (no unrestricted filesystem traversal).
    spawn_config_dir: str = ""

    # Governance & Policy
    spawn_policy_webhook_url: str = ""
    spawn_policy_webhook_secret: str = ""
    spawn_policy_webhook_timeout_s: float = 2.0

    # Auto-shutdown when all MCP clients disconnect and no agents are active.
    auto_shutdown_enabled: bool = False
    auto_shutdown_grace_s: float = 30.0

    # Paths that never require auth.
    auth_public_paths: list[str] = Field(
        default_factory=lambda: ["/", "/dashboard", "/hijack", "/hijack/", "/hijack/hijack.html"]
    )
    auth_public_prefixes: list[str] = Field(default_factory=lambda: ["/static/", "/hijack/assets/"])
