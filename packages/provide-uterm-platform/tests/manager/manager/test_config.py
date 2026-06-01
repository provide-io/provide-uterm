#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.manager.config."""

from provide.uterm.manager.config import ManagerConfig


class TestManagerConfig:
    def test_defaults(self):
        cfg = ManagerConfig()
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 2272
        assert cfg.max_agents == 200
        assert cfg.log_level == "info"
        assert cfg.worker_env_prefix == "UTERM_"
        assert cfg.auth_token_env_var == "UTERM_MANAGER_API_TOKEN"
        assert cfg.auth_worker_token_env_var == "UTERM_MANAGER_WORKER_TOKEN"
        assert len(cfg.auth_public_paths) > 0
        assert len(cfg.auth_public_prefixes) > 0

    def test_custom_values(self):
        cfg = ManagerConfig(
            title="My Manager",
            host="0.0.0.0",
            port=9999,
            max_agents=50,
            state_file="/tmp/state.json",
            worker_env_prefix="MYBOT_",
        )
        assert cfg.title == "My Manager"
        assert cfg.port == 9999
        assert cfg.worker_env_prefix == "MYBOT_"
