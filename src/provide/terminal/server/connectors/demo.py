#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Interactive in-memory reference connector for the hosted server."""

from __future__ import annotations

import hashlib
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from provide.terminal.server.connectors.base import SessionConnector

_COLS = 80
_ROWS = 25


@dataclass(slots=True)
class _Entry:
    speaker: str
    text: str
    ts: float


class DemoSessionConnector(SessionConnector):
    """Reference connector that behaves like a lightweight interactive session."""

    def __init__(self, session_id: str, display_name: str) -> None:
        self._session_id = session_id
        self._display_name = display_name
        self._connected = False
        self._input_mode = "open"
        self._paused = False
        self._turns = 0
        self._nickname = "user"
        self._last_command: str | None = None
        self._banner = "Ready. Type /help for commands."
        self._transcript: deque[_Entry] = deque(maxlen=10)
        self._reset_state()

    def _reset_state(self) -> None:
        self._paused = False
        self._turns = 0
        self._nickname = "user"
        self._last_command = None
        self._banner = "Ready. Type /help for commands."
        self._transcript = deque(
            [
                _Entry("system", "Session online.", time.time()),
                _Entry("session", "Use /help, /mode open, /mode hijack, /clear, /status, /reset.", time.time()),
            ],
            maxlen=10,
        )

    @staticmethod
    def _normalize_input(data: str) -> str:
        return data.replace("\r", "\n").replace("\t", " ").strip()

    def _append(self, speaker: str, text: str) -> None:
        self._transcript.append(_Entry(speaker, text, time.time()))

    def _mode_label(self) -> str:
        return "Shared input" if self._input_mode == "open" else "Exclusive hijack"

    def _control_label(self) -> str:
        return "Paused for hijack" if self._paused else "Live"

    def _prompt(self) -> str:
        return f"{self._nickname}> "

    def _render_screen(self) -> str:
        lines = [
            f"\x1b[1;36m[{self._display_name} ({self._session_id})]\x1b[0m",
            "-" * 60,
            f"\x1b[32mMode:\x1b[0m {self._mode_label()}",
            f"\x1b[32mControl:\x1b[0m {self._control_label()}",
            "\x1b[32mHelp:\x1b[0m /help /mode open|hijack /clear /nick /say /status /demo /reset",
            f"\x1b[33m{self._banner}\x1b[0m",
            "",
            "\x1b[1mTranscript\x1b[0m",
        ]
        lines.extend(f"{entry.speaker:>7}: {entry.text}" for entry in self._transcript)
        lines.append("")
        lines.append(self._prompt())
        return "\n".join(lines[-_ROWS:])

    def _snapshot(self) -> dict[str, Any]:
        screen = self._render_screen()
        last_line = (screen.splitlines() or [""])[-1]
        cursor_x = min(len(last_line), _COLS - 1)
        cursor_y = min(len(screen.splitlines()) - 1, _ROWS - 1)
        return {
            "type": "snapshot",
            "screen": screen,
            "cursor": {"x": cursor_x, "y": cursor_y},
            "cols": _COLS,
            "rows": _ROWS,
            "screen_hash": hashlib.sha256(screen.encode("utf-8")).hexdigest()[:16],
            "cursor_at_end": True,
            "has_trailing_space": False,
            "prompt_detected": {"prompt_id": "demo_prompt"},
            "ts": time.time(),
        }

    def _hello(self) -> dict[str, Any]:
        return {"type": "worker_hello", "input_mode": self._input_mode, "ts": time.time()}

    async def start(self) -> None:
        self._connected = True

    async def stop(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    async def poll_messages(self) -> list[dict[str, Any]]:
        return []

    async def handle_input(self, data: str) -> list[dict[str, Any]]:
        text = self._normalize_input(data)
        if not text:
            self._banner = "Empty input ignored."
            return [self._snapshot()]
        self._turns += 1
        if text.startswith("/"):
            command, _, rest = text.partition(" ")
            arg = rest.strip()
            self._last_command = command
            if command == "/help":
                self._banner = "Command help printed below."
                self._append(
                    "system",
                    "Commands: /help /clear /mode open|hijack /status /nick <name> /say <text> /demo /reset",
                )
                return [self._snapshot()]
            if command == "/clear":
                self._transcript = deque(maxlen=10)
                self._banner = "Transcript cleared."
                return [self._snapshot()]
            if command == "/mode":
                mode = arg.lower()
                if mode not in {"open", "hijack"}:
                    self._banner = "Usage: /mode open|hijack"
                    self._append("system", "usage: /mode open|hijack")
                    return [self._snapshot()]
                return await self.set_mode(mode)
            if command == "/status":
                self._banner = "Session status printed below."
                self._append("system", f"mode={self._input_mode} paused={self._paused} turns={self._turns}")
                return [self._snapshot()]
            if command == "/nick":
                if not arg:
                    self._banner = "Usage: /nick <name>"
                    self._append("system", "usage: /nick <name>")
                    return [self._snapshot()]
                self._nickname = arg[:24]
                self._banner = f"Nickname set to {self._nickname}."
                self._append("system", f"nickname: {self._nickname}")
                return [self._snapshot()]
            if command == "/say":
                if not arg:
                    self._banner = "Usage: /say <text>"
                    self._append("system", "usage: /say <text>")
                    return [self._snapshot()]
                self._banner = "Message appended."
                self._append("user", f"{self._nickname}: {arg}")
                return [self._snapshot()]
            if command == "/demo":
                self._banner = "Demo response appended."
                self._append("session", "This hosted server is the reference implementation.")
                return [self._snapshot()]
            if command == "/reset":
                self._reset_state()
                self._banner = "Session reset."
                return [self._hello(), self._snapshot()]
            self._banner = f"Unknown command: {command}"
            self._append("system", f"unknown command: {command}")
            return [self._snapshot()]
        self._banner = "Input accepted."
        self._append("user", f"{self._nickname}: {text}")
        self._append("session", f'session: received "{text}"')
        return [self._snapshot()]

    async def handle_control(self, action: str) -> list[dict[str, Any]]:
        if action == "pause":
            self._paused = True
            self._banner = "Exclusive control active. Input is still accepted."
            self._append("system", "control: hijack acquired")
        elif action == "resume":
            self._paused = False
            self._banner = "Exclusive control released."
            self._append("system", "control: released")
        elif action == "step":
            self._turns += 1
            self._banner = "Single-step acknowledged."
            self._append("system", f"control: single step #{self._turns}")
        else:
            self._banner = f"Ignored unknown control action: {action}"
            self._append("system", f"control: ignored {action}")
        return [self._snapshot()]

    async def get_snapshot(self) -> dict[str, Any]:
        return self._snapshot()

    async def get_analysis(self) -> str:
        return "\n".join(
            [
                f"[interactive demo analysis — worker: {self._session_id}]",
                f"input_mode: {self._input_mode}",
                f"paused: {self._paused}",
                f"turn_counter: {self._turns}",
                f"transcript_entries: {len(self._transcript)}",
                f"last_command: {self._last_command or '(none)'}",
                f"prompt_visible: {bool(self._prompt().strip())}",
            ]
        )

    async def set_mode(self, mode: str) -> list[dict[str, Any]]:
        if mode not in {"open", "hijack"}:
            raise ValueError(f"invalid mode: {mode}")
        self._input_mode = mode
        if mode == "open":
            self._paused = False
        self._banner = f"Input mode set to {self._mode_label()}."
        self._append("system", f"mode: {self._mode_label()}")
        return [self._hello(), self._snapshot()]
