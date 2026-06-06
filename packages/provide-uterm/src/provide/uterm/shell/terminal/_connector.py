#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""UshellConnector — SessionConnector implementation for provide.uterm.shell.

This connector implements the same interface as ``ShellSessionConnector``,
``SSHSessionConnector``, etc., allowing ushell to be used anywhere a
``SessionConnector`` is accepted — most importantly with
``HostedSessionRuntime`` (FastAPI server).

For the Cloudflare DO runtime, :mod:`provide_uterm_cloudflare.do.ushell`
imports this class and drives it inline (no external process or WebSocket).

Input model
-----------
``handle_input()`` receives raw keystroke bytes exactly as xterm.js sends
them.  A :class:`~provide.uterm.shell._repl.LineBuffer` accumulates them
into lines, echoes printable characters, and handles backspace / Ctrl+C.
Commands are executed only on Enter; partial lines produce only echo frames.

Output model
------------
All output is returned as a list of ``{"type": "term", "data": "..."}``
worker-protocol frames.  Callers broadcast these to connected browsers.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

try:
    from provide.uterm.server.connectors.base import SessionConnector as _SessionConnector
except ImportError:  # pragma: no cover
    _SessionConnector = object  # type: ignore[assignment,misc]  # ty:ignore[invalid-assignment]

from provide.uterm.shell._output import BANNER, CLEAR_SCREEN, PROMPT
from provide.uterm.shell._repl import LineBuffer
from provide.uterm.shell._sandbox import Sandbox
from provide.uterm.shell.commands import AnimatedResult, CommandDispatcher
from provide.uterm.shell.terminal._output import term, worker_hello


class UshellConnector(_SessionConnector):
    """Interactive Python REPL connector that needs no external process.

    Args:
        session_id:   Worker / session identifier (used in analysis output).
        display_name: Human-readable name (unused in output; reserved).
        _config:      Optional connector config dict (currently unused).
        extra_ctx:    Extra names injected into the sandbox and command
                      dispatcher context.  Pass CF bindings here (e.g.
                      ``{"env": cf_env, "list_kv_sessions": fn}``).
    """

    def __init__(
        self,
        session_id: str = "",  # pragma: no mutate
        display_name: str = "",  # pragma: no mutate
        _config: dict[str, Any] | None = None,
        extra_ctx: dict[str, Any] | None = None,
    ) -> None:
        self._session_id = session_id
        self._display_name = display_name or session_id
        self._connected = False
        self._welcomed = False
        self._buf = LineBuffer()
        self._sandbox = Sandbox(extra=extra_ctx)
        ctx: dict[str, Any] = dict(extra_ctx or {})
        self._dispatcher = CommandDispatcher(ctx, self._sandbox)
        self._pending_frames: list[dict[str, Any]] = []
        self._animation_task: asyncio.Task[None] | None = None
        # Tier-A backpressure: when flow-paused, poll_messages withholds output so
        # the DO stops forwarding to congested browsers (the CF SessionRuntime sends
        # flow_pause/flow_resume via handle_control). Distinct from hijack pause.
        self._flow_paused = False

    # ------------------------------------------------------------------
    # SessionConnector lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._connected = True

    async def stop(self) -> None:
        self._connected = False
        if self._animation_task and not self._animation_task.done():
            self._animation_task.cancel()

    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Message production
    # ------------------------------------------------------------------

    async def poll_messages(self) -> list[dict[str, Any]]:
        """Return the initial welcome frames on first call after connect.

        Returns a ``worker_hello`` frame (sets ``input_mode=open`` so all
        operators can type) followed by the banner + first prompt.
        Subsequent calls sleep briefly so the HostedSessionRuntime's
        recv_task can win the poll/recv race and deliver browser input.
        """
        if not self._connected:
            return []
        if self._flow_paused:
            return []
        if not self._welcomed:
            self._welcomed = True
            return [
                worker_hello("open"),
                term(BANNER + PROMPT),
            ]
        if self._pending_frames:
            frames = list(self._pending_frames)
            self._pending_frames.clear()
            return frames
        await asyncio.sleep(0.05)
        return []

    async def handle_input(self, data: str) -> list[dict[str, Any]]:
        """Process raw keystroke *data* and return terminal frames."""
        self._buf.feed(data)
        frames: list[dict[str, Any]] = []

        echo = self._buf.take_echo()
        if echo:
            frames.append(term(echo))

        for line in self._buf.take_completed():
            result = await self._dispatcher.dispatch(line)
            if isinstance(result, AnimatedResult):
                if self._animation_task and not self._animation_task.done():
                    self._animation_task.cancel()
                self._animation_task = asyncio.create_task(self._stream_animation(result))
            else:
                frames.extend(term(s) for s in result)

        return frames

    async def _stream_animation(self, result: AnimatedResult) -> None:
        """Stream animation frames as terminal output."""
        delay = 1.0 / result.fps if result.fps > 0 else 0.1
        try:
            while True:
                for frame in result.frames:
                    await asyncio.sleep(delay)
                    self._pending_frames.append(term(frame))
                if not result.loop:
                    break
        except asyncio.CancelledError:
            pass
        self._pending_frames.append(term(PROMPT))

    async def handle_control(self, action: str) -> list[dict[str, Any]]:
        """Handle control actions.

        Hijack ``pause``/``resume``/``step`` are ignored — the REPL is always
        interactive. Tier-A backpressure ``flow_pause``/``flow_resume`` gate
        ``poll_messages`` output, and ``snapshot_request`` returns a fresh
        snapshot to resync a recovered viewer.
        """
        if action == "flow_pause":
            self._flow_paused = True
        elif action == "flow_resume":
            self._flow_paused = False
        elif action == "snapshot_request":
            return [await self.get_snapshot()]
        return []

    # ------------------------------------------------------------------
    # Snapshot / analysis
    # ------------------------------------------------------------------

    async def get_snapshot(self) -> dict[str, Any]:
        current = self._buf.current_line()
        screen = f"ushell {self._session_id}\r\n{PROMPT}{current}"
        return {
            "type": "snapshot",
            "screen": screen,
            "cursor": {"x": len(PROMPT) + len(current), "y": 1},
            "cols": 80,
            "rows": 24,
            "screen_hash": str(hash(screen))[:16],
            "cursor_at_end": True,
            "has_trailing_space": False,
            "prompt_detected": {"prompt_id": "ushell_prompt"},
            "ts": time.time(),
        }

    async def get_analysis(self) -> str:
        return "\n".join(
            [
                f"[ushell analysis — session: {self._session_id}]",
                f"connected: {self._connected}",
                f"current_line: {self._buf.current_line()!r}",
                f"sandbox_names: {sorted(k for k in self._sandbox.namespace if not k.startswith('__'))}",
            ]
        )

    # ------------------------------------------------------------------
    # Clear / mode
    # ------------------------------------------------------------------

    async def clear(self) -> list[dict[str, Any]]:
        self._buf.clear()
        return [term(CLEAR_SCREEN + PROMPT)]

    async def set_mode(self, mode: str) -> list[dict[str, Any]]:
        # Ushell is always open mode; silently accept set_mode calls.
        return [worker_hello(mode)]
