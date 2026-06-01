#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Spawn-env token scoping for the External Management Tier (item 5b).

A spawned worker subprocess must NOT inherit the omnipotent operator token.
When a low-privilege worker token is configured, the worker's manager API
token env var is rewritten to carry only the worker token, and the operator
token / raw worker-token env var are stripped from the child environment.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from provide.uterm.manager.config import ManagerConfig
from provide.uterm.manager.core import AgentManager
from provide.uterm.manager.process import AgentProcessManager

OPERATOR_VAR = "UTERM_MANAGER_API_TOKEN"
WORKER_VAR = "UTERM_MANAGER_WORKER_TOKEN"


class FakeWorkerPlugin:
    @property
    def worker_type(self) -> str:
        return "test_game"

    @property
    def worker_module(self) -> str:
        return "test_worker_module"

    def configure_worker_env(self, env, agent_status, manager, **kwargs):
        env["TEST_CUSTOM"] = "value"


@pytest.fixture
def config(tmp_path):
    return ManagerConfig(
        state_file=str(tmp_path / "state.json"),
        timeseries_dir=str(tmp_path / "metrics"),
        log_dir=str(tmp_path / "logs"),
    )


@pytest.fixture
def manager(config):
    return AgentManager(config)


@pytest.fixture
def pm(manager, tmp_path):
    pm = AgentProcessManager(
        manager,
        worker_registry={"test_game": FakeWorkerPlugin()},
        log_dir=str(tmp_path / "logs"),
    )
    manager.agent_process_manager = pm
    return pm


def _build(pm, manager):
    env_prefix = manager.config.worker_env_prefix
    return pm._build_worker_env(env_prefix, None, FakeWorkerPlugin(), {})


class TestWorkerEnvTokenScoping:
    def test_worker_token_set_rewrites_api_token_and_strips_worker_var(self, pm, manager) -> None:
        with patch.dict(
            os.environ,
            {OPERATOR_VAR: "operator-secret", WORKER_VAR: "worker-secret"},
            clear=False,
        ):
            env = _build(pm, manager)
        # The child sees only the low-priv token under the API-token name.
        assert env[OPERATOR_VAR] == "worker-secret"
        assert env[OPERATOR_VAR] != "operator-secret"
        # The raw worker-token var is not leaked into the child.
        assert WORKER_VAR not in env

    def test_worker_token_unset_leaves_operator_token_in_env(self, pm, manager) -> None:
        """Backward compat: no worker token configured → operator token forwarded as before."""
        with patch.dict(os.environ, {OPERATOR_VAR: "operator-secret"}, clear=False):
            os.environ.pop(WORKER_VAR, None)
            env = _build(pm, manager)
        assert env[OPERATOR_VAR] == "operator-secret"

    def test_no_operator_token_no_worker_token_no_token_keys(self, pm, manager) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(OPERATOR_VAR, None)
            os.environ.pop(WORKER_VAR, None)
            env = _build(pm, manager)
        assert OPERATOR_VAR not in env
        assert WORKER_VAR not in env

    def test_worker_token_whitespace_treated_as_unset(self, pm, manager) -> None:
        with patch.dict(
            os.environ,
            {OPERATOR_VAR: "operator-secret", WORKER_VAR: "   "},
            clear=False,
        ):
            env = _build(pm, manager)
        # Whitespace worker token is not a real token → operator token preserved.
        assert env[OPERATOR_VAR] == "operator-secret"
        assert WORKER_VAR not in env
