#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for server connectors: ShellSessionConnector, TelnetSessionConnector, SshSessionConnector."""

from __future__ import annotations

from typing import Any

import pytest

# ---------------------------------------------------------------------------
# ShellSessionConnector
# ---------------------------------------------------------------------------


class TestShellSessionConnector:
    def _make(self, config: dict[str, Any] | None = None) -> Any:
        from provide.terminal.server.connectors.shell import ShellSessionConnector

        return ShellSessionConnector("sess1", "Test Shell", config)

    @pytest.mark.asyncio
    async def test_start_stop_connected(self) -> None:
        c = self._make()
        assert not c.is_connected()
        await c.start()
        assert c.is_connected()
        await c.stop()
        assert not c.is_connected()

    @pytest.mark.asyncio
    async def test_poll_messages_returns_empty(self) -> None:
        c = self._make()
        await c.start()
        assert await c.poll_messages() == []

    @pytest.mark.asyncio
    async def test_get_snapshot_shape(self) -> None:
        c = self._make()
        await c.start()
        snap = await c.get_snapshot()
        assert snap["type"] == "snapshot"
        assert isinstance(snap["screen"], str)
        assert snap["cols"] == 80
        assert snap["rows"] == 25

    @pytest.mark.asyncio
    async def test_get_analysis(self) -> None:
        c = self._make()
        analysis = await c.get_analysis()
        assert "sess1" in analysis
        assert "input_mode" in analysis

    @pytest.mark.asyncio
    async def test_handle_input_plain_text(self) -> None:
        c = self._make()
        await c.start()
        msgs = await c.handle_input("hello world")
        assert any(m["type"] == "snapshot" for m in msgs)
        screen = msgs[-1]["screen"]
        assert "hello world" in screen

    @pytest.mark.asyncio
    async def test_handle_input_empty(self) -> None:
        c = self._make()
        await c.start()
        msgs = await c.handle_input("   ")
        assert msgs[-1]["type"] == "snapshot"
        assert "Empty input" in msgs[-1]["screen"]

    @pytest.mark.asyncio
    async def test_handle_input_cmd_help(self) -> None:
        c = self._make()
        await c.start()
        msgs = await c.handle_input("/help")
        assert msgs[-1]["type"] == "snapshot"
        assert "help" in msgs[-1]["screen"].lower()

    @pytest.mark.asyncio
    async def test_handle_input_cmd_clear(self) -> None:
        c = self._make()
        await c.start()
        await c.handle_input("some text")
        msgs = await c.handle_input("/clear")
        assert msgs[-1]["type"] == "snapshot"

    @pytest.mark.asyncio
    async def test_handle_input_cmd_mode_open(self) -> None:
        c = self._make({"input_mode": "hijack"})
        await c.start()
        msgs = await c.handle_input("/mode open")
        types = [m["type"] for m in msgs]
        assert "worker_hello" in types
        assert msgs[-1]["screen"].count("Shared input") >= 1

    @pytest.mark.asyncio
    async def test_handle_input_cmd_mode_hijack(self) -> None:
        c = self._make()
        await c.start()
        msgs = await c.handle_input("/mode hijack")
        types = [m["type"] for m in msgs]
        assert "worker_hello" in types

    @pytest.mark.asyncio
    async def test_handle_input_cmd_mode_invalid(self) -> None:
        c = self._make()
        await c.start()
        msgs = await c.handle_input("/mode bogus")
        assert "Usage" in msgs[-1]["screen"] or "usage" in msgs[-1]["screen"]

    @pytest.mark.asyncio
    async def test_handle_input_cmd_status(self) -> None:
        c = self._make()
        await c.start()
        msgs = await c.handle_input("/status")
        assert msgs[-1]["type"] == "snapshot"
        assert "mode=" in msgs[-1]["screen"]

    @pytest.mark.asyncio
    async def test_handle_input_cmd_nick(self) -> None:
        c = self._make()
        await c.start()
        msgs = await c.handle_input("/nick alice")
        assert "alice" in msgs[-1]["screen"]

    @pytest.mark.asyncio
    async def test_handle_input_cmd_nick_empty(self) -> None:
        c = self._make()
        await c.start()
        msgs = await c.handle_input("/nick")
        assert "Usage" in msgs[-1]["screen"] or "usage" in msgs[-1]["screen"]

    @pytest.mark.asyncio
    async def test_handle_input_cmd_say(self) -> None:
        c = self._make()
        await c.start()
        msgs = await c.handle_input("/say hello there")
        assert "hello there" in msgs[-1]["screen"]

    @pytest.mark.asyncio
    async def test_handle_input_cmd_say_empty(self) -> None:
        c = self._make()
        await c.start()
        msgs = await c.handle_input("/say")
        assert "Usage" in msgs[-1]["screen"] or "usage" in msgs[-1]["screen"]

    @pytest.mark.asyncio
    async def test_handle_input_cmd_shell(self) -> None:
        c = self._make()
        await c.start()
        msgs = await c.handle_input("/shell")
        assert msgs[-1]["type"] == "snapshot"

    @pytest.mark.asyncio
    async def test_handle_input_cmd_reset(self) -> None:
        c = self._make()
        await c.start()
        await c.handle_input("some text")
        msgs = await c.handle_input("/reset")
        types = [m["type"] for m in msgs]
        assert "worker_hello" in types

    @pytest.mark.asyncio
    async def test_handle_input_cmd_unknown(self) -> None:
        c = self._make()
        await c.start()
        msgs = await c.handle_input("/boguscmd")
        assert "Unknown command" in msgs[-1]["screen"] or "unknown command" in msgs[-1]["screen"]

    @pytest.mark.asyncio
    async def test_handle_control_pause_resume(self) -> None:
        c = self._make()
        await c.start()
        msgs = await c.handle_control("pause")
        assert msgs[-1]["type"] == "snapshot"
        assert "Paused" in msgs[-1]["screen"]
        msgs = await c.handle_control("resume")
        assert "Live" in msgs[-1]["screen"]

    @pytest.mark.asyncio
    async def test_handle_control_step(self) -> None:
        c = self._make()
        await c.start()
        msgs = await c.handle_control("step")
        assert msgs[-1]["type"] == "snapshot"

    @pytest.mark.asyncio
    async def test_handle_control_unknown(self) -> None:
        c = self._make()
        await c.start()
        msgs = await c.handle_control("explode")
        assert msgs[-1]["type"] == "snapshot"
        assert "explode" in msgs[-1]["screen"]

    @pytest.mark.asyncio
    async def test_clear(self) -> None:
        c = self._make()
        await c.start()
        await c.handle_input("some text")
        msgs = await c.clear()
        assert msgs[-1]["type"] == "snapshot"

    @pytest.mark.asyncio
    async def test_set_mode_open(self) -> None:
        c = self._make({"input_mode": "hijack"})
        await c.start()
        msgs = await c.set_mode("open")
        assert any(m["type"] == "worker_hello" for m in msgs)
        assert msgs[-1]["type"] == "snapshot"

    @pytest.mark.asyncio
    async def test_set_mode_hijack(self) -> None:
        c = self._make()
        await c.start()
        msgs = await c.set_mode("hijack")
        assert any(m["type"] == "worker_hello" for m in msgs)

    @pytest.mark.asyncio
    async def test_set_mode_invalid_raises(self) -> None:
        c = self._make()
        with pytest.raises(ValueError, match="invalid mode"):
            await c.set_mode("superuser")

    @pytest.mark.asyncio
    async def test_set_mode_open_clears_paused(self) -> None:
        c = self._make({"input_mode": "hijack"})
        await c.start()
        await c.handle_control("pause")
        await c.set_mode("open")
        # After switching to open, paused should be False — screen shows "Live"
        snap = await c.get_snapshot()
        assert "Live" in snap["screen"]


# (TelnetSessionConnector and SshSessionConnector tests moved to test_connectors_2.py)
