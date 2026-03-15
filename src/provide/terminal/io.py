#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Generic I/O patterns for BBS terminal interaction.

Provides :class:`PromptWaiter` and :class:`InputSender` — reusable primitives
for wait-for-prompt and send-input logic with timeout and idle detection.
No game-specific logic is included.
"""

from __future__ import annotations

import asyncio
import time
from inspect import isawaitable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable


class Session(Protocol):
    """Minimal interface expected by :class:`PromptWaiter` and :class:`InputSender`."""

    async def wait_for_update(self, *, timeout_ms: int, since: int | None = None) -> bool:
        """Wait until new bytes arrive from the remote (or until timeout)."""
        ...

    def snapshot(self) -> dict[str, Any]:
        """Return latest snapshot without performing network I/O."""
        ...

    async def send(self, data: str) -> None:
        """Send data to the session."""
        ...


async def _session_is_connected(session: Any) -> bool:
    """Return connection state for sync or async ``is_connected`` implementations."""
    checker = getattr(session, "is_connected", None)
    if checker is None:
        return True
    value = checker() if callable(checker) else checker
    if isawaitable(value):
        value = await value
    return bool(value)


class PromptWaiter:
    """Wait for a prompt to appear in the session snapshot.

    Args:
        session: BBS session object conforming to :class:`Session`.
        on_screen_update: Optional callback invoked with the raw screen text on each poll.
    """

    def __init__(
        self,
        session: Session | None,
        on_screen_update: Callable[[str], None] | None = None,
    ) -> None:
        self.session = session
        self.on_screen_update = on_screen_update

    async def wait_for_prompt(
        self,
        expected_prompt_id: str | None = None,
        timeout_ms: int = 10000,
        read_interval_ms: int = 250,
        on_prompt_detected: Callable[[dict[str, Any]], bool] | None = None,
        on_prompt_seen: Callable[[dict[str, Any]], None] | None = None,
        on_prompt_rejected: Callable[[dict[str, Any], str], None] | None = None,
        require_idle: bool = True,
        idle_grace_ratio: float = 0.8,
    ) -> dict[str, Any]:
        """Poll the session until a matching prompt is detected.

        Args:
            expected_prompt_id: If set, only accept prompts whose ``prompt_id`` contains this string.
            timeout_ms: Maximum wait time in milliseconds.
            read_interval_ms: Polling interval used as a backstop timer.
            on_prompt_detected: Optional filter callback; return ``True`` to accept the prompt.
            on_prompt_seen: Optional callback fired for every candidate prompt.
            on_prompt_rejected: Optional callback fired when a candidate is rejected.
            require_idle: Wait for screen to stabilise before returning.
            idle_grace_ratio: Accept non-idle prompt after this fraction of timeout has elapsed.

        Returns:
            Dict with keys: ``screen``, ``prompt_id``, ``input_type``, ``kv_data``, ``is_idle``.

        Raises:
            TimeoutError: If no matching prompt detected within ``timeout_ms``.
            ConnectionError: If session is ``None`` or disconnected.
        """
        start_mono = time.monotonic()
        timeout_sec = timeout_ms / 1000.0
        read_interval_sec = read_interval_ms / 1000.0

        while time.monotonic() - start_mono < timeout_sec:
            if self.session is None:
                raise ConnectionError("Session is None")
            if not await _session_is_connected(self.session):
                raise ConnectionError("Session disconnected")

            snapshot = self.session.snapshot()
            screen = snapshot.get("screen", "")

            if self.on_screen_update:
                self.on_screen_update(screen)

            if "prompt_detected" in snapshot:
                detected = snapshot["prompt_detected"]
                detected_full = dict(detected or {})
                detected_full["screen"] = screen
                detected_full["screen_hash"] = snapshot.get("screen_hash", "")
                detected_full["captured_at"] = snapshot.get("captured_at")
                prompt_id = str(detected.get("prompt_id") or "")
                is_idle = detected.get("is_idle", False)

                if on_prompt_seen:
                    on_prompt_seen(detected_full)

                elapsed = time.monotonic() - start_mono
                if require_idle and not is_idle and elapsed < timeout_sec * idle_grace_ratio:
                    if on_prompt_rejected:
                        on_prompt_rejected(detected_full, "not_idle")
                    remaining_idle = getattr(self.session, "seconds_until_idle", lambda _t=2.0: read_interval_sec)()
                    wait_ms = int(max(1, min(remaining_idle, timeout_sec - elapsed) * 1000))
                    await self.session.wait_for_update(timeout_ms=wait_ms)
                    continue

                if expected_prompt_id and expected_prompt_id not in prompt_id:
                    if on_prompt_rejected:
                        on_prompt_rejected(detected_full, "expected_mismatch")
                    await self.session.wait_for_update(timeout_ms=int(read_interval_sec * 1000))
                    continue

                if on_prompt_detected and not on_prompt_detected(detected_full):
                    if on_prompt_rejected:
                        on_prompt_rejected(detected_full, "callback_reject")
                    await self.session.wait_for_update(timeout_ms=int(read_interval_sec * 1000))
                    continue

                return {
                    "screen": screen,
                    "prompt_id": prompt_id,
                    "input_type": detected.get("input_type"),
                    "kv_data": detected_full.get("kv_data"),
                    "is_idle": is_idle,
                }

            remaining = max(0, timeout_sec - (time.monotonic() - start_mono))
            await self.session.wait_for_update(timeout_ms=int(min(read_interval_sec, remaining) * 1000))

        raise TimeoutError(f"No prompt detected within {timeout_ms}ms")


class InputSender:
    """Send keystrokes to a session respecting input type semantics.

    Args:
        session: BBS session object conforming to :class:`Session`.
    """

    def __init__(self, session: Session | None) -> None:
        self.session = session

    async def send_input(
        self,
        keys: str,
        input_type: str | None = "multi_key",
        wait_after_sec: float = 0.2,
    ) -> None:
        """Send input respecting prompt type.

        - ``single_key``: sends keys as-is (no newline).
        - ``multi_key``: appends ``\\r``.
        - ``any_key``: sends a single space.
        - anything else: treated as ``multi_key``.

        Args:
            keys: The text/keys to send.
            input_type: Prompt input type.
            wait_after_sec: Seconds to sleep after sending (0 to skip).

        Raises:
            ConnectionError: If session is ``None`` or disconnected.
        """
        if self.session is None:
            raise ConnectionError("Session is None")
        if not await _session_is_connected(self.session):
            raise ConnectionError("Session disconnected")

        if input_type == "single_key":
            await self.session.send(keys)
        elif input_type == "any_key":
            await self.session.send(" ")
        else:
            await self.session.send(keys + "\r")

        if wait_after_sec > 0:
            await asyncio.sleep(wait_after_sec)
