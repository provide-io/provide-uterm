#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""DeckMux control transfer — handover, auto-transfer, keystroke queue."""

from __future__ import annotations

from typing import Any

from provide.terminal.deckmux._protocol import (
    KeystrokeQueueMode,
    TransferReason,
    encode_keys_display,
    make_control_transfer,
)

MAX_QUEUE_LENGTH = 256


class TransferManager:
    """Control transfer logic. Does not own the lease — calls back to the
    hijack ownership system for actual acquire/release."""

    def __init__(
        self,
        auto_transfer_idle_s: float = 30.0,
        keystroke_queue_mode: KeystrokeQueueMode = "display",
    ) -> None:
        self._auto_idle_s = auto_transfer_idle_s
        self._queue_mode = keystroke_queue_mode
        self._queues: dict[str, str] = {}  # user_id -> raw key buffer
        self._warning_sent: bool = False

    @property
    def auto_transfer_enabled(self) -> bool:
        """Whether auto-transfer on idle is enabled."""
        return self._auto_idle_s > 0

    @property
    def queue_mode(self) -> KeystrokeQueueMode:
        """Current keystroke queue mode."""
        return self._queue_mode

    def queue_keystroke(self, user_id: str, raw_keys: str) -> str:
        """Buffer keystrokes for a non-owner. Returns display string."""
        buf = self._queues.get(user_id, "")
        buf += raw_keys
        if len(buf) > MAX_QUEUE_LENGTH:
            buf = buf[-MAX_QUEUE_LENGTH:]
        self._queues[user_id] = buf
        return encode_keys_display(buf)

    def flush_queue(self, user_id: str) -> str:
        """Remove and return the raw keystroke buffer for a user."""
        return self._queues.pop(user_id, "")

    def clear_queue(self, user_id: str) -> None:
        """Remove the keystroke buffer for a user without returning it."""
        self._queues.pop(user_id, "")

    def get_queue_display(self, user_id: str) -> str:
        """Get the display-format keystroke queue for a user."""
        raw = self._queues.get(user_id, "")
        return encode_keys_display(raw) if raw else ""

    def check_auto_transfer(
        self,
        owner_idle_s: float,
        queued_users: list[str],
    ) -> tuple[bool, bool]:
        """Check if auto-transfer should happen.

        Returns (should_warn, should_transfer).
        should_warn: True if within 10s of threshold and not yet warned.
        should_transfer: True if threshold exceeded and someone is queued.
        """
        if not self.auto_transfer_enabled or not queued_users:
            self._warning_sent = False
            return False, False

        warn_threshold = max(0, self._auto_idle_s - 10)

        if owner_idle_s >= self._auto_idle_s:
            self._warning_sent = False
            return False, True

        if owner_idle_s >= warn_threshold and not self._warning_sent:
            self._warning_sent = True
            return True, False

        return False, False

    def reset_warning(self) -> None:
        """Reset the warning-sent flag (call when owner becomes active)."""
        self._warning_sent = False

    def build_transfer_message(
        self,
        from_user: str,
        to_user: str,
        reason: TransferReason,
    ) -> dict[str, Any]:
        """Build a control_transfer message, handling the keystroke queue."""
        queued = ""
        if self._queue_mode == "replay":
            queued = self.flush_queue(to_user)
        else:
            display = self.get_queue_display(to_user)
            self.clear_queue(to_user)
            queued = display  # display-only: show what was typed but don't replay
        return make_control_transfer(from_user, to_user, reason, queued)
