#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.deckmux._transfer — control transfer and keystroke queue."""

from __future__ import annotations

from provide.uterm.deckmux._transfer import MAX_QUEUE_LENGTH, TransferManager

# --- Properties ---


def test_auto_transfer_enabled_default() -> None:
    tm = TransferManager()
    assert tm.auto_transfer_enabled is True


def test_auto_transfer_disabled() -> None:
    tm = TransferManager(auto_transfer_idle_s=0)
    assert tm.auto_transfer_enabled is False


def test_auto_transfer_disabled_negative() -> None:
    tm = TransferManager(auto_transfer_idle_s=-1)
    assert tm.auto_transfer_enabled is False


def test_queue_mode_default() -> None:
    tm = TransferManager()
    assert tm.queue_mode == "display"


def test_queue_mode_replay() -> None:
    tm = TransferManager(keystroke_queue_mode="replay")
    assert tm.queue_mode == "replay"


# --- Keystroke queueing ---


def test_queue_keystroke_basic() -> None:
    tm = TransferManager()
    display = tm.queue_keystroke("u1", "ls")
    assert display == "ls"


def test_queue_keystroke_accumulates() -> None:
    tm = TransferManager()
    tm.queue_keystroke("u1", "l")
    display = tm.queue_keystroke("u1", "s")
    assert display == "ls"


def test_queue_keystroke_with_special_keys() -> None:
    tm = TransferManager()
    display = tm.queue_keystroke("u1", "ls\r")
    assert display == "ls↵"


def test_queue_keystroke_overflow_truncates() -> None:
    tm = TransferManager()
    long_input = "a" * (MAX_QUEUE_LENGTH + 50)
    tm.queue_keystroke("u1", long_input)
    # The raw buffer should be truncated to MAX_QUEUE_LENGTH
    display = tm.get_queue_display("u1")
    assert len(display) == MAX_QUEUE_LENGTH


def test_flush_queue() -> None:
    tm = TransferManager()
    tm.queue_keystroke("u1", "hello")
    raw = tm.flush_queue("u1")
    assert raw == "hello"
    # Queue should be empty now
    assert tm.flush_queue("u1") == ""


def test_flush_queue_empty() -> None:
    tm = TransferManager()
    assert tm.flush_queue("u1") == ""


def test_clear_queue() -> None:
    tm = TransferManager()
    tm.queue_keystroke("u1", "hello")
    tm.clear_queue("u1")
    assert tm.get_queue_display("u1") == ""


def test_clear_queue_nonexistent() -> None:
    tm = TransferManager()
    tm.clear_queue("nonexistent")  # should not raise


def test_get_queue_display_empty() -> None:
    tm = TransferManager()
    assert tm.get_queue_display("u1") == ""


def test_get_queue_display_with_data() -> None:
    tm = TransferManager()
    tm.queue_keystroke("u1", "ls\r")
    assert tm.get_queue_display("u1") == "ls↵"


# --- Auto-transfer checks ---


def test_check_auto_transfer_disabled() -> None:
    tm = TransferManager(auto_transfer_idle_s=0)
    warn, transfer = tm.check_auto_transfer(999, ["u2"])
    assert warn is False
    assert transfer is False


def test_check_auto_transfer_no_queued_users() -> None:
    tm = TransferManager(auto_transfer_idle_s=30)
    warn, transfer = tm.check_auto_transfer(999, [])
    assert warn is False
    assert transfer is False


def test_check_auto_transfer_should_transfer() -> None:
    tm = TransferManager(auto_transfer_idle_s=30)
    warn, transfer = tm.check_auto_transfer(30, ["u2"])
    assert warn is False
    assert transfer is True


def test_check_auto_transfer_should_transfer_exceeds() -> None:
    tm = TransferManager(auto_transfer_idle_s=30)
    warn, transfer = tm.check_auto_transfer(45, ["u2"])
    assert warn is False
    assert transfer is True


def test_check_auto_transfer_should_warn() -> None:
    tm = TransferManager(auto_transfer_idle_s=30)
    # Owner idle for 20s (>= 30-10=20), should warn
    warn, transfer = tm.check_auto_transfer(20, ["u2"])
    assert warn is True
    assert transfer is False


def test_check_auto_transfer_warn_only_once() -> None:
    tm = TransferManager(auto_transfer_idle_s=30)
    warn1, _ = tm.check_auto_transfer(22, ["u2"])
    assert warn1 is True
    # Second check at same idle — should NOT warn again
    warn2, _ = tm.check_auto_transfer(25, ["u2"])
    assert warn2 is False


def test_check_auto_transfer_below_warn_threshold() -> None:
    tm = TransferManager(auto_transfer_idle_s=30)
    warn, transfer = tm.check_auto_transfer(15, ["u2"])
    assert warn is False
    assert transfer is False


def test_check_auto_transfer_warn_resets_on_no_queued() -> None:
    tm = TransferManager(auto_transfer_idle_s=30)
    # Trigger warning
    tm.check_auto_transfer(22, ["u2"])
    # No queued users resets warning
    tm.check_auto_transfer(22, [])
    # Now warning should fire again
    warn, _ = tm.check_auto_transfer(22, ["u2"])
    assert warn is True


def test_check_auto_transfer_warn_resets_on_disabled() -> None:
    tm = TransferManager(auto_transfer_idle_s=0)
    # Disabled — always returns False, resets warning
    warn, transfer = tm.check_auto_transfer(999, ["u2"])
    assert warn is False
    assert transfer is False


def test_check_auto_transfer_small_threshold() -> None:
    """With threshold < 10, warn_threshold clamps to 0."""
    tm = TransferManager(auto_transfer_idle_s=5)
    # idle 3s: >= max(0, 5-10)=0, so should warn
    warn, transfer = tm.check_auto_transfer(3, ["u2"])
    assert warn is True
    assert transfer is False


def test_check_auto_transfer_transfer_resets_warning() -> None:
    """Transfer resets the warning flag."""
    tm = TransferManager(auto_transfer_idle_s=30)
    # First: warn
    tm.check_auto_transfer(22, ["u2"])
    # Then: transfer
    _, transfer = tm.check_auto_transfer(30, ["u2"])
    assert transfer is True
    # Warning should be reset, can warn again
    warn, _ = tm.check_auto_transfer(22, ["u2"])
    assert warn is True


# --- reset_warning ---


def test_reset_warning() -> None:
    tm = TransferManager(auto_transfer_idle_s=30)
    tm.check_auto_transfer(22, ["u2"])  # triggers warning
    tm.reset_warning()
    warn, _ = tm.check_auto_transfer(22, ["u2"])
    assert warn is True  # can warn again after reset


# --- build_transfer_message ---


def test_build_transfer_message_display_mode() -> None:
    tm = TransferManager(keystroke_queue_mode="display")
    tm.queue_keystroke("u2", "ls\r")
    msg = tm.build_transfer_message("u1", "u2", "handover")
    assert msg["type"] == "control_transfer"
    assert msg["from_user_id"] == "u1"
    assert msg["to_user_id"] == "u2"
    assert msg["reason"] == "handover"
    assert msg["queued_keys"] == "ls↵"  # display format
    # Queue should be cleared
    assert tm.get_queue_display("u2") == ""


def test_build_transfer_message_replay_mode() -> None:
    tm = TransferManager(keystroke_queue_mode="replay")
    tm.queue_keystroke("u2", "ls\r")
    msg = tm.build_transfer_message("u1", "u2", "auto_idle")
    assert msg["queued_keys"] == "ls\r"  # raw keys for replay
    # Queue should be flushed
    assert tm.flush_queue("u2") == ""


def test_build_transfer_message_empty_queue() -> None:
    tm = TransferManager()
    msg = tm.build_transfer_message("u1", "u2", "admin_takeover")
    assert msg["queued_keys"] == ""


def test_build_transfer_message_replay_empty_queue() -> None:
    tm = TransferManager(keystroke_queue_mode="replay")
    msg = tm.build_transfer_message("u1", "u2", "lease_expired")
    assert msg["queued_keys"] == ""


# --- Default value killers ---


def test_default_auto_transfer_idle_is_30() -> None:
    """Default idle threshold is exactly 30.0, not 31.0."""
    tm = TransferManager()
    # At 30s idle with queued users, transfer should trigger
    _, transfer = tm.check_auto_transfer(30.0, ["u2"])
    assert transfer is True
    # At 29s, should NOT trigger (would if threshold were 29 or less)
    tm2 = TransferManager()
    _, transfer2 = tm2.check_auto_transfer(29.0, ["u2"])
    assert transfer2 is False


def test_default_queue_mode_is_display_not_replay() -> None:
    """Default queue mode is 'display'; flush_queue returns raw only in replay mode."""
    tm = TransferManager()
    tm.queue_keystroke("u1", "ls\r")
    msg = tm.build_transfer_message("u2", "u1", "handover")
    # display mode: queued_keys is encoded display, NOT raw bytes
    assert msg["queued_keys"] == "ls↵"


def test_warning_sent_initializes_as_false_not_none() -> None:
    """_warning_sent must be False (bool), not None, for truthiness logic to work."""
    tm = TransferManager()
    assert tm._warning_sent is False
    assert tm._warning_sent == False  # noqa: E712 — explicit not-None check


def test_warn_threshold_max_uses_0_not_1() -> None:
    """max(0, ...) not max(1, ...): at idle_s=5, warn threshold=0, idle=0.5 should warn."""
    tm = TransferManager(auto_transfer_idle_s=5)
    warn, _ = tm.check_auto_transfer(0.5, ["u2"])
    # max(0, 5-10) = 0, 0.5 >= 0 → warn
    # max(1, 5-10) = 1, 0.5 < 1 → would NOT warn
    assert warn is True


def test_warn_threshold_uses_minus_10_not_minus_11() -> None:
    """warn_threshold = idle_s - 10; at idle=20, threshold=20, should warn."""
    tm = TransferManager(auto_transfer_idle_s=30)
    # 30 - 10 = 20: at exactly 20s idle, warn should fire
    warn, _ = tm.check_auto_transfer(20.0, ["u2"])
    assert warn is True
    # But at 19s, should NOT warn (would if threshold were 19)
    tm2 = TransferManager(auto_transfer_idle_s=30)
    warn2, _ = tm2.check_auto_transfer(19.0, ["u2"])
    assert warn2 is False


def test_flush_queue_missing_returns_empty_string_not_none() -> None:
    """flush_queue on unknown user returns '', not None."""
    tm = TransferManager()
    result = tm.flush_queue("nonexistent")
    assert result == ""
    assert result is not None


def test_get_queue_display_missing_returns_empty_string_not_none() -> None:
    """get_queue_display on unknown user returns '', not None."""
    tm = TransferManager()
    result = tm.get_queue_display("nonexistent")
    assert result == ""
    assert result is not None


def test_queue_overflow_uses_strict_greater_than() -> None:
    """Buffer truncates when len > MAX_QUEUE_LENGTH, not >= (i.e., MAX_QUEUE_LENGTH is allowed)."""
    tm = TransferManager()
    # Exactly MAX_QUEUE_LENGTH chars — should NOT be truncated
    exact = "a" * MAX_QUEUE_LENGTH
    tm.queue_keystroke("u1", exact)
    raw = tm.flush_queue("u1")
    assert len(raw) == MAX_QUEUE_LENGTH
