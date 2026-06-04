#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for manager/process_impl — SpawnAgent."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.manager.config import ManagerConfig
from provide.uterm.manager.core import AgentManager
from provide.uterm.manager.process import AgentProcessManager


class FakeWorkerPlugin:
    @property
    def worker_type(self) -> str:
        return "test_game"

    @property
    def worker_module(self) -> str:
        return "test_module"

    def configure_worker_env(self, env, agent_status, manager, **kwargs):
        env["CONFIGURED"] = "yes"


@pytest.fixture
def config(tmp_path):
    return ManagerConfig(
        state_file=str(tmp_path / "state.json"),
        timeseries_dir=str(tmp_path / "metrics"),
        log_dir=str(tmp_path / "logs"),
        health_check_interval_s=0,
        heartbeat_timeout_s=1,
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


def make_mock_proc(pid=42, returncode=0):
    m = MagicMock()
    m.pid = pid
    m.returncode = returncode
    m.poll.return_value = None
    m.wait.return_value = returncode
    return m


class TestSpawnAgentKills:
    # ---- helpers -------------------------------------------------------
    @staticmethod
    def _cfg(tmp_path, text="worker_type: test_game\n"):
        p = tmp_path / "cfg.yaml"
        p.write_text(text)
        return str(p)

    # ---- guard clauses -------------------------------------------------
    @pytest.mark.asyncio
    async def test_note_agent_id_called_with_real_id(self, pm, manager, tmp_path):
        """m1: note_agent_id(None) -> _next_agent_index not advanced."""
        cfg = self._cfg(tmp_path)
        manager.broadcast_status = AsyncMock()
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            await pm.spawn_agent(cfg, "agent_005")
        # note_agent_id(agent_005) must bump _next_agent_index to >= 6.
        assert pm._next_agent_index >= 6

    @pytest.mark.asyncio
    async def test_max_agents_uses_ge_not_gt(self, pm, manager, tmp_path):
        """m2: >= -> > would allow one over the configured limit."""
        from provide.uterm.manager.models import AgentStatusBase

        manager.max_agents = 1
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000")
        cfg = self._cfg(tmp_path)
        with pytest.raises(RuntimeError, match="Max agents"):
            await pm.spawn_agent(cfg, "agent_001")

    @pytest.mark.asyncio
    async def test_max_agents_error_message_preserved(self, pm, manager, tmp_path):
        """m3: RuntimeError(None) -> message becomes 'None'."""
        from provide.uterm.manager.models import AgentStatusBase

        manager.max_agents = 1
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000")
        cfg = self._cfg(tmp_path)
        with pytest.raises(RuntimeError, match=r"Max agents \(1\) reached"):
            await pm.spawn_agent(cfg, "agent_001")

    @pytest.mark.asyncio
    async def test_config_not_found_error_message_preserved(self, pm, manager):
        """m6: RuntimeError(None) -> message becomes 'None'."""
        with pytest.raises(RuntimeError, match="Config not found: /does/not/exist.yaml"):
            await pm.spawn_agent("/does/not/exist.yaml", "agent_000")

    # ---- logger.info("spawning_agent", ...) ----------------------------
    @pytest.mark.asyncio
    async def test_spawning_agent_info_log_args(self, pm, manager, tmp_path):
        """m7/8/9/11/12/13/14: logger.info first positional + agent_id/config_path kwargs."""
        # spawn_agent was extracted into process_impl_spawn; its module-level
        # ``logger`` lives there now, so patch.object targets that module.
        import provide.uterm.manager.process_impl_spawn as mod

        cfg = self._cfg(tmp_path)
        manager.broadcast_status = AsyncMock()
        fake_logger = MagicMock()
        with patch.object(mod, "logger", fake_logger):
            with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
                await pm.spawn_agent(cfg, "agent_009")

        spawning = [
            (c.args, c.kwargs) for c in fake_logger.info.call_args_list if c.args and c.args[0] == "spawning_agent"
        ]
        assert spawning, f"no spawning_agent info log: {fake_logger.info.call_args_list}"
        a, k = spawning[0]
        assert a[0] == "spawning_agent"
        assert k.get("agent_id") == "agent_009"
        assert k.get("config_path") == cfg

    # ---- _load_worker_type(config_path) --------------------------------
    @pytest.mark.asyncio
    async def test_load_worker_type_gets_config_path(self, pm, manager, tmp_path):
        """m16: _load_worker_type(None) -> read fails -> worker_type 'default'.

        With a second registry entry the 'default' single-entry fallback no
        longer applies, so the mutant raises 'Unknown worker_type'. The
        original reads the real config_path, resolves 'test_game', succeeds.
        """
        cfg = self._cfg(tmp_path, "worker_type: test_game\n")
        pm._worker_registry["other"] = FakeWorkerPlugin()
        manager.broadcast_status = AsyncMock()
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            result = await pm.spawn_agent(cfg, "agent_000")
        assert result == "agent_000"

    # ---- policy gate intercept_spawn(agent_id, config_path, raw) -------
    @pytest.mark.asyncio
    async def test_policy_gate_receives_all_three_args(self, pm, manager, tmp_path):
        """m18/19/20: intercept_spawn positional args agent_id/config_path/raw."""
        cfg = self._cfg(tmp_path, "worker_type: test_game\nfoo: bar\n")
        manager.broadcast_status = AsyncMock()
        seen = {}

        class RecordingGate:
            async def intercept_spawn(self, agent_id, config_path, raw):
                seen["agent_id"] = agent_id
                seen["config_path"] = config_path
                seen["raw"] = raw
                return True

        pm.set_policy_gate(RecordingGate())
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            await pm.spawn_agent(cfg, "agent_003")
        assert seen["agent_id"] == "agent_003"
        assert seen["config_path"] == cfg
        assert seen["raw"] == {"worker_type": "test_game", "foo": "bar"}

    # ---- logger.warning on policy reject -------------------------------
    @pytest.mark.asyncio
    async def test_policy_reject_warning_log_and_error(self, pm, manager, tmp_path):
        """m24/25/27/28/29: logger.warning('agent_spawn_rejected_by_policy', agent_id=...)."""
        # spawn_agent was extracted into process_impl_spawn; its module-level
        # ``logger`` lives there now, so patch.object targets that module.
        import provide.uterm.manager.process_impl_spawn as mod

        cfg = self._cfg(tmp_path)

        class DenyGate:
            async def intercept_spawn(self, agent_id, config_path, raw):
                return False

        pm.set_policy_gate(DenyGate())
        fake_logger = MagicMock()
        with patch.object(mod, "logger", fake_logger):
            with pytest.raises(RuntimeError, match="Spawn rejected by policy for agent agent_003"):
                await pm.spawn_agent(cfg, "agent_003")

        rej = [
            (c.args, c.kwargs)
            for c in fake_logger.warning.call_args_list
            if c.args and c.args[0] == "agent_spawn_rejected_by_policy"
        ]
        assert rej, f"missing rejection warning: {fake_logger.warning.call_args_list}"
        assert rej[0][1].get("agent_id") == "agent_003"

    # ---- _get_registry_entry(worker_type, config_path) ----------------
    @pytest.mark.asyncio
    async def test_registry_error_message_includes_config_path(self, pm, manager, tmp_path):
        """m33: _get_registry_entry(worker_type, None) -> config_path dropped from error msg."""
        cfg = self._cfg(tmp_path, "worker_type: not_registered\n")
        pm._worker_registry["other"] = FakeWorkerPlugin()
        with pytest.raises(RuntimeError, match=r"not_registered"):
            await pm.spawn_agent(cfg, "agent_000")
        # The config_path must appear in the message (None mutation drops it).
        try:
            await pm.spawn_agent(cfg, "agent_000")
        except RuntimeError as e:
            assert cfg in str(e)

    # ---- worker_module + cmd list --------------------------------------
    @pytest.mark.asyncio
    async def test_cmd_exact_contents(self, pm, manager, tmp_path):
        """m36/38/39/40/41/42/43: worker_module=None & cmd string literals."""
        import sys

        cfg = self._cfg(tmp_path)
        manager.broadcast_status = AsyncMock()
        captured = {}

        def fake_spawn(bid, cmd, env):
            captured["cmd"] = list(cmd)
            return make_mock_proc()

        with patch.object(pm, "_spawn_process", side_effect=fake_spawn):
            await pm.spawn_agent(cfg, "agent_007")

        assert captured["cmd"] == [
            sys.executable,
            "-m",
            "test_module",
            "--config",
            cfg,
            "--agent-id",
            "agent_007",
        ]

    # ---- agent_entry resolution + _build_worker_env --------------------
    @pytest.mark.asyncio
    async def test_agent_entry_passed_so_configure_worker_env_runs(self, pm, manager, tmp_path):
        """m45/46/49: agent_entry None -> configure_worker_env skipped -> env lacks CONFIGURED."""
        from provide.uterm.manager.models import AgentStatusBase

        cfg = self._cfg(tmp_path)
        manager.broadcast_status = AsyncMock()
        # Pre-register the agent so agents.get(agent_id) is truthy.
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000")
        captured = {}

        def fake_spawn(bid, cmd, env):
            captured["env"] = dict(env)
            return make_mock_proc()

        with patch.object(pm, "_spawn_process", side_effect=fake_spawn):
            await pm.spawn_agent(cfg, "agent_000")

        assert captured["env"].get("CONFIGURED") == "yes"

    @pytest.mark.asyncio
    async def test_raw_config_forwarded_to_configure_worker_env(self, pm, manager, tmp_path):
        """m51: _build_worker_env(..., raw->None, ...) drops raw_config kwarg."""
        from provide.uterm.manager.models import AgentStatusBase

        cfg = self._cfg(tmp_path, "worker_type: test_game\nmagic: 7\n")
        manager.broadcast_status = AsyncMock()
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000")

        class RawRecordingPlugin(FakeWorkerPlugin):
            def configure_worker_env(self, env, agent_status, manager, **kwargs):
                raw = kwargs.get("raw_config")
                env["RAW_MAGIC"] = str(raw.get("magic")) if raw else "MISSING"

        pm._worker_registry["test_game"] = RawRecordingPlugin()
        captured = {}

        def fake_spawn(bid, cmd, env):
            captured["env"] = dict(env)
            return make_mock_proc()

        with patch.object(pm, "_spawn_process", side_effect=fake_spawn):
            await pm.spawn_agent(cfg, "agent_000")

        assert captured["env"].get("RAW_MAGIC") == "7"

    @pytest.mark.asyncio
    async def test_agent_id_forwarded_to_build_worker_env(self, pm, manager, tmp_path):
        """m52: _build_worker_env(..., agent_id->None) breaks token derivation."""
        import os

        from provide.uterm.manager.auth import derive_agent_token

        cfg = self._cfg(tmp_path)
        manager.broadcast_status = AsyncMock()
        operator_var = manager.config.auth_token_env_var
        worker_var = manager.config.auth_worker_token_env_var
        captured = {}

        def fake_spawn(bid, cmd, env):
            captured["env"] = dict(env)
            return make_mock_proc()

        expected = derive_agent_token("fleet-secret", "agent_000")
        with patch.dict(os.environ, {worker_var: "fleet-secret"}, clear=False):
            with patch.object(pm, "_spawn_process", side_effect=fake_spawn):
                await pm.spawn_agent(cfg, "agent_000")

        assert captured["env"].get(operator_var) == expected

    # ---- update-branch state writes ------------------------------------
    @pytest.mark.asyncio
    async def test_update_branch_records_pid(self, pm, manager, tmp_path):
        """m68: update-branch pid=None."""
        from provide.uterm.manager.models import AgentStatusBase

        cfg = self._cfg(tmp_path)
        manager.broadcast_status = AsyncMock()
        manager.agents["agent_000"] = AgentStatusBase(agent_id="agent_000")
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc(pid=4242)):
            await pm.spawn_agent(cfg, "agent_000")
        assert manager.agents["agent_000"].pid == 4242
        assert manager.agents["agent_000"].state == "running"

    # ---- insert-branch (no pre-existing agent) -------------------------
    @pytest.mark.asyncio
    async def test_insert_branch_records_pid(self, pm, manager, tmp_path):
        """m77/83: insert-branch pid=None / pid arg dropped -> defaults to None."""
        cfg = self._cfg(tmp_path)
        manager.broadcast_status = AsyncMock()
        assert "agent_000" not in manager.agents
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc(pid=4242)):
            await pm.spawn_agent(cfg, "agent_000")
        assert manager.agents["agent_000"].pid == 4242
        assert manager.agents["agent_000"].config == cfg

    @pytest.mark.asyncio
    async def test_insert_branch_seeds_last_update_time(self, pm, manager, tmp_path):
        """m87: insert-branch drops last_update_time arg -> defaults to 0.0."""
        cfg = self._cfg(tmp_path)
        manager.broadcast_status = AsyncMock()
        assert "agent_000" not in manager.agents
        with patch.object(pm, "_spawn_process", return_value=make_mock_proc()):
            await pm.spawn_agent(cfg, "agent_000")
        assert manager.agents["agent_000"].last_update_time > 0.0

    # ---- exception path: logger.exception("agent_spawn_failed", ...) ---
    @pytest.mark.asyncio
    async def test_spawn_failure_exception_log_and_raise(self, pm, manager, tmp_path):
        """m100/101/102/104/105/106/107/108: logger.exception args + RuntimeError wrap."""
        # spawn_agent was extracted into process_impl_spawn; its module-level
        # ``logger`` lives there now, so patch.object targets that module.
        import provide.uterm.manager.process_impl_spawn as mod

        cfg = self._cfg(tmp_path)
        manager.broadcast_status = AsyncMock()
        boom = ValueError("kaboom-unique-123")
        fake_logger = MagicMock()

        with patch.object(mod, "logger", fake_logger):
            with patch.object(pm, "_spawn_process", side_effect=boom):
                with pytest.raises(RuntimeError, match="Failed to spawn agent: kaboom-unique-123"):
                    await pm.spawn_agent(cfg, "agent_011")

        failed = [
            (c.args, c.kwargs)
            for c in fake_logger.exception.call_args_list
            if c.args and c.args[0] == "agent_spawn_failed"
        ]
        assert failed, f"missing failure log: {fake_logger.exception.call_args_list}"
        a, k = failed[0]
        assert a[0] == "agent_spawn_failed"
        assert k.get("agent_id") == "agent_011"
        assert k.get("error") == "kaboom-unique-123"


# ---- _build_preexec_rlimit_fn (35 killed, 0 equiv) ----
