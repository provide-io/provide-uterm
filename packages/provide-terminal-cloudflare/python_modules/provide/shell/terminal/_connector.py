#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""UshellConnector — SessionConnector implementation for provide.shell.

This connector implements the same interface as ``ShellSessionConnector``,
``SSHSessionConnector``, etc., allowing ushell to be used anywhere a
``SessionConnector`` is accepted — most importantly with
``HostedSessionRuntime`` (FastAPI server).

For the Cloudflare DO runtime, :mod:`provide_terminal_cloudflare.do.ushell`
imports this class and drives it inline (no external process or WebSocket).

Input model
-----------
``handle_input()`` receives raw keystroke bytes exactly as xterm.js sends
them.  A :class:`~provide.shell._repl.LineBuffer` accumulates them
into lines, echoes printable characters, and handles backspace / Ctrl+C.
Commands are executed only on Enter; partial lines produce only echo frames.

Output model
------------
All output is returned as a list of ``{"type": "term", "data": "..."}``
worker-protocol frames.  Callers broadcast these to connected browsers.
"""

from __future__ import annotations

import time
from typing import Any

try:
    from provide.terminal.server.connectors.base import SessionConnector as _SessionConnector
except ImportError:
    _SessionConnector = object  # type: ignore[assignment,misc]

from provide.shell._commands import CommandDispatcher
from provide.shell._output import BANNER, CLEAR_SCREEN, PROMPT
from provide.shell._repl import LineBuffer
from provide.shell._sandbox import Sandbox
from provide.shell.terminal._output import term, worker_hello


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
        session_id: str = "",
        display_name: str = "",
        _config: dict[str, Any] | None = None,
        extra_ctx: dict[str, Any] | None = None,
    ) -> None:
        self._session_id = session_id
        self._display_name = display_name or session_id
        self._connected = False
        self._buf = LineBuffer()
        self._sandbox = Sandbox(extra=extra_ctx)
        ctx: dict[str, Any] = dict(extra_ctx or {})
        self._dispatcher = CommandDispatcher(ctx, self._sandbox)

    # ------------------------------------------------------------------
    # SessionConnector lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._connected = True

    async def stop(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Message production
    # ------------------------------------------------------------------

    async def poll_messages(self) -> list[dict[str, Any]]:
        """Return the initial welcome frames emitted on first connect.

        Returns a ``worker_hello`` frame (sets ``input_mode=open`` so all
        operators can type) followed by the banner + first prompt.
        """
        if not self._connected:
            return []
        return [
            worker_hello("open"),
            term(BANNER + PROMPT),
        ]

    async def handle_input(self, data: str) -> list[dict[str, Any]]:
        """Process raw keystroke *data* and return terminal frames.

        Echo frames are emitted for every printable character.  Command
        output frames are emitted only when Enter is pressed.
        """
        self._buf.feed(data)
        frames: list[dict[str, Any]] = []

        echo = self._buf.take_echo()
        if echo:
            frames.append(term(echo))

        for line in self._buf.take_completed():
            output_strings = await self._dispatcher.dispatch(line)
            frames.extend(term(s) for s in output_strings)

        return frames

    async def handle_control(self, action: str) -> list[dict[str, Any]]:  # noqa: ARG002
        """Handle hijack control actions.

        Ushell ignores ``pause``/``resume``/``step`` — the REPL is always
        interactive and doesn't distinguish paused state.
        """
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
