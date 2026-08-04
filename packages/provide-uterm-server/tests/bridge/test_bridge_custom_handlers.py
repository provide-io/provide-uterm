#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for TermBridge.register_message_handler — the public hook that lets
consumers add app-specific message types without subclassing _dispatch_msg."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from provide.uterm.control_channel import encode_control_frame
from provide.uterm.server.bridge.worker_link import TermBridge


class MockSession:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.sizes: list[tuple[int, int]] = []
        self._watches: list[Any] = []
        self.emulator = MagicMock()
        self.emulator.get_snapshot.return_value = {"screen": "x", "cols": 80, "rows": 25}

    def add_watch(self, fn: Any, *, interval_s: float) -> None:
        self._watches.append(fn)

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def set_size(self, cols: int, rows: int) -> None:
        self.sizes.append((cols, rows))


class MockBot:
    def __init__(self, session: MockSession | None = None) -> None:
        self.session = session
        self.hijacked_calls: list[bool] = []
        self.step_calls: int = 0

    async def set_hijacked(self, enabled: bool) -> None:
        self.hijacked_calls.append(enabled)

    async def request_step(self) -> None:
        self.step_calls += 1


class MockWS:
    def __init__(self, messages: list[str] | None = None) -> None:
        self.sent: list[str] = []
        self._messages = list(messages or [])
        self._idx = 0

    async def recv(self) -> str:
        if self._idx >= len(self._messages):
            raise Exception("WebSocket closed")
        msg = self._messages[self._idx]
        self._idx += 1
        return msg

    async def send(self, data: str) -> None:
        self.sent.append(data)


class TestRegisterMessageHandler:
    async def test_custom_handler_fires_for_registered_type(self) -> None:
        bot = MockBot()
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = True

        seen: list[dict[str, Any]] = []

        async def _on_analyze(msg: dict[str, Any]) -> None:
            seen.append(msg)

        bridge.register_message_handler("analyze_req", _on_analyze)

        ws = MockWS([encode_control_frame({"type": "analyze_req", "payload": {"k": 1}})])
        await bridge._recv_loop(ws)

        assert len(seen) == 1
        assert seen[0]["type"] == "analyze_req"
        assert seen[0]["payload"] == {"k": 1}

    async def test_custom_handler_not_invoked_for_builtin_types(self) -> None:
        """Built-in types (snapshot_req, control, resize) must NOT route to custom handlers."""
        session = MockSession()
        bot = MockBot(session)
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = True

        custom_calls: list[str] = []

        async def _spy(msg: dict[str, Any]) -> None:
            custom_calls.append(str(msg.get("type")))

        # Register a handler for EVERY built-in type name to prove built-ins win.
        for builtin in ("snapshot_req", "control", "resize"):
            bridge.register_message_handler(builtin, _spy)

        ws = MockWS(
            [
                encode_control_frame({"type": "snapshot_req"}),
                encode_control_frame({"type": "control", "action": "step"}),
                encode_control_frame({"type": "resize", "cols": 100, "rows": 30}),
            ]
        )
        await bridge._recv_loop(ws)

        # Custom spy must NEVER fire — built-ins took precedence.
        assert custom_calls == []
        # And the built-ins produced their normal side-effects.
        assert bot.step_calls == 1
        assert session.sizes == [(100, 30)]
        # snapshot_req queued a snapshot behind any preceding terminal output.
        assert ws.sent == []
        snapshot = bridge._send_q.get_nowait()
        assert snapshot["type"] == "snapshot"

    async def test_unknown_type_with_no_handler_is_silently_ignored(self) -> None:
        """Backward-compat: unknown types without a registered handler don't raise."""
        bot = MockBot()
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = True

        ws = MockWS([encode_control_frame({"type": "totally_unknown", "x": 1})])
        # Must complete cleanly with no exception.
        await bridge._recv_loop(ws)

    async def test_re_register_replaces_handler(self) -> None:
        bot = MockBot()
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = True

        first: list[dict[str, Any]] = []
        second: list[dict[str, Any]] = []

        async def _first(msg: dict[str, Any]) -> None:
            first.append(msg)

        async def _second(msg: dict[str, Any]) -> None:
            second.append(msg)

        bridge.register_message_handler("custom", _first)
        bridge.register_message_handler("custom", _second)

        ws = MockWS([encode_control_frame({"type": "custom"})])
        await bridge._recv_loop(ws)

        assert first == []
        assert len(second) == 1

    async def test_custom_handler_exception_does_not_kill_loop(self) -> None:
        bot = MockBot()
        bridge = TermBridge(bot, "bot1", "http://localhost:8000")
        bridge._running = True

        async def _boom(msg: dict[str, Any]) -> None:
            raise RuntimeError("intentional")

        seen: list[dict[str, Any]] = []

        async def _ok(msg: dict[str, Any]) -> None:
            seen.append(msg)

        bridge.register_message_handler("explode", _boom)
        bridge.register_message_handler("good", _ok)

        ws = MockWS(
            [
                encode_control_frame({"type": "explode"}),
                encode_control_frame({"type": "good", "n": 7}),
            ]
        )
        await bridge._recv_loop(ws)

        # The exploding handler did not tear down the recv loop —
        # the next message still routed to its handler.
        assert len(seen) == 1
        assert seen[0]["n"] == 7
