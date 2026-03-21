# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Mutation-killing tests for BotProcessManager — spawn_swarm, release, account management."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSpawnSwarm:
    @pytest.mark.asyncio
    async def test_name_style_stored_on_pm(self, pm, manager, tmp_path):
        """Kills spawn_swarm mutations that skip storing name_style."""
        config = tmp_path / "test.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        with patch.object(pm, "spawn_bot", new_callable=AsyncMock, return_value="bot_000"):
            await pm.spawn_swarm([str(config)], name_style="fixed", name_base="mybot")

        assert pm._spawn_name_style == "fixed"
        assert pm._spawn_name_base == "mybot"

    @pytest.mark.asyncio
    async def test_queued_bots_pre_registered(self, pm, manager, tmp_path):
        """Kills spawn_swarm mutations that skip pre-registration."""
        config = tmp_path / "test.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()

        pre_states: dict[str, str] = {}

        # Capture states right before first spawn to verify pre-registration happened
        orig_spawn = pm.spawn_bot
        first_call = [True]

        async def capture_first_call(cfg, bid):
            if first_call[0]:
                first_call[0] = False
                # At this point both bots should already be pre-registered as queued
                for b_id, b in manager.bots.items():
                    if b_id not in pre_states:
                        pre_states[b_id] = b.state
            return await orig_spawn(cfg, bid)

        mock_proc = MagicMock()
        mock_proc.pid = 1

        with (
            patch.object(pm, "spawn_bot", side_effect=capture_first_call),
            patch.object(pm, "_spawn_process", return_value=mock_proc),
        ):
            await pm.spawn_swarm([str(config), str(config)], group_size=2)

        # At first spawn, there should be pre-registered bots
        assert len(pre_states) >= 2
        # They should all have been queued initially
        for bid, state in pre_states.items():
            assert state == "queued", f"Bot {bid} was {state}, expected queued"

    @pytest.mark.asyncio
    async def test_group_end_min_calc(self, pm, manager, tmp_path):
        """Kills spawn_swarm mutations in group_end calculation."""
        config = tmp_path / "test.yaml"
        config.write_text("worker_type: test_game\n")
        manager.broadcast_status = AsyncMock()
        mock_proc = MagicMock()
        mock_proc.pid = 1

        # With 3 configs and group_size=2, should spawn all 3
        with patch.object(pm, "_spawn_process", return_value=mock_proc):
            result = await pm.spawn_swarm([str(config)] * 3, group_size=2, group_delay=0.0)

        assert len(result) == 3


# ---------------------------------------------------------------------------
# release_bot_account - cooldown_s=0
# ---------------------------------------------------------------------------
class TestReleaseBotAccount:
    def test_cooldown_s_is_zero(self, pm, manager):
        """Kills mutations changing cooldown_s value."""
        pool = MagicMock()
        pool.release_by_bot.return_value = True
        manager.account_pool = pool

        pm.release_bot_account("bot_000")

        pool.release_by_bot.assert_called_once_with(bot_id="bot_000", cooldown_s=0)
