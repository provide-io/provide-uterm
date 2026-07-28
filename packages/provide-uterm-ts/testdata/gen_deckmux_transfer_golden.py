#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript DeckMux transfer.

Control transfer decides who is actually typing into a live terminal, so each
rule here has a consequence somebody watching would notice.

* **The queue is bounded, keeping the newest.** Somebody who cannot type yet
  still generates keystrokes; keeping the oldest would show them the start of
  what they typed minutes ago instead of what they just typed.
* **Warn once, then transfer.** A warning re-sent on every check is a
  notification storm; one never re-armed means the next idle period passes
  silently.
* **Nobody waiting means nothing happens.** Auto-transfer with no queued user
  would hand control to nobody and leave the terminal orphaned.
* **Display versus replay.** In display mode the queued keys are *shown* and
  dropped; in replay mode they are handed over to be typed. Confusing the two
  either loses the keystrokes or executes them twice.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_deckmux_transfer_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.deckmux import _transfer as transfer_module
from provide.uterm.deckmux._transfer import TransferManager

OUT = Path(__file__).with_name("deckmux_transfer_golden.json")

# (name, chunks) — keystrokes buffered for somebody who cannot type yet.
QUEUE_CASES: list[tuple[str, list[str]]] = [
    ("a single chunk", ["ls"]),
    ("several chunks", ["l", "s", " -la"]),
    ("an arrow key", ["\x1b[A"]),
    ("exactly the bound", ["x" * transfer_module.MAX_QUEUE_LENGTH]),
    ("one over the bound", ["x" * (transfer_module.MAX_QUEUE_LENGTH + 1)]),
    ("well over the bound", ["a" * 200, "b" * 200]),
    ("empty", [""]),
]

# (name, idle, queued) — when the manager warns, and when it hands over.
AUTO_CASES: list[tuple[str, float, list[str]]] = [
    ("nobody waiting", 100.0, []),
    ("well within the window", 5.0, ["u2"]),
    ("exactly at the warning threshold", 20.0, ["u2"]),
    ("between warning and transfer", 25.0, ["u2"]),
    ("exactly at the transfer threshold", 30.0, ["u2"]),
    ("past the transfer threshold", 45.0, ["u2"]),
]


def _record_queues() -> list[dict[str, Any]]:
    """What the queue holds, and what it shows."""
    records = []
    for name, chunks in QUEUE_CASES:
        manager = TransferManager()
        displays = [manager.queue_keystroke("u2", chunk) for chunk in chunks]
        raw = manager.flush_queue("u2")
        records.append(
            {
                "name": name,
                "chunks": chunks,
                "displays": displays,
                "raw_length": len(raw),
                "raw_tail": raw[-8:],
                "after_flush": manager.get_queue_display("u2"),
            }
        )
    return records


def _record_auto() -> list[dict[str, Any]]:
    """Whether a fresh manager warns or transfers at each idle time."""
    records = []
    for name, idle, queued in AUTO_CASES:
        manager = TransferManager()
        warn, transfer = manager.check_auto_transfer(idle, queued)
        records.append({"name": name, "idle": idle, "queued": queued, "warn": warn, "transfer": transfer})
    return records


def _record_warning_sequence() -> dict[str, Any]:
    """The warning is sent once, and re-armed by the right things."""
    manager = TransferManager()
    first = manager.check_auto_transfer(25.0, ["u2"])
    second = manager.check_auto_transfer(26.0, ["u2"])
    manager.reset_warning()
    after_reset = manager.check_auto_transfer(27.0, ["u2"])

    # An empty queue re-arms it too: the owner is no longer holding anybody up.
    rearmed = TransferManager()
    rearmed.check_auto_transfer(25.0, ["u2"])
    rearmed.check_auto_transfer(25.0, [])
    after_empty_queue = rearmed.check_auto_transfer(25.0, ["u2"])

    # And so does the transfer itself, so the next idle period warns again.
    cycled = TransferManager()
    cycled.check_auto_transfer(25.0, ["u2"])
    cycled.check_auto_transfer(45.0, ["u2"])
    after_transfer = cycled.check_auto_transfer(25.0, ["u2"])

    return {
        "first": list(first),
        "second": list(second),
        "after_reset": list(after_reset),
        "after_empty_queue": list(after_empty_queue),
        "after_transfer": list(after_transfer),
    }


def _record_modes() -> dict[str, Any]:
    """What a transfer message carries in each queue mode."""
    display = TransferManager(keystroke_queue_mode="display")
    display.queue_keystroke("u2", "ls\r")
    display_message = display.build_transfer_message("u1", "u2", "handover")

    replay = TransferManager(keystroke_queue_mode="replay")
    replay.queue_keystroke("u2", "ls\r")
    replay_message = replay.build_transfer_message("u1", "u2", "handover")

    empty = TransferManager()
    empty_message = empty.build_transfer_message("u1", "u2", "auto_idle")

    # Somebody else's queue is not handed to the new owner.
    other = TransferManager(keystroke_queue_mode="replay")
    other.queue_keystroke("u3", "rm -rf /\r")
    other_message = other.build_transfer_message("u1", "u2", "handover")

    return {
        "display": display_message,
        "display_queue_after": display.get_queue_display("u2"),
        "replay": replay_message,
        "replay_queue_after": replay.get_queue_display("u2"),
        "empty": empty_message,
        "someone_elses_queue": other_message,
        "someone_elses_queue_survives": other.get_queue_display("u3"),
    }


def _record_settings() -> dict[str, Any]:
    """The knobs and their defaults."""
    default = TransferManager()
    disabled = TransferManager(auto_transfer_idle_s=0)
    negative = TransferManager(auto_transfer_idle_s=-1)
    short = TransferManager(auto_transfer_idle_s=5)
    return {
        "default_mode": default.queue_mode,
        "default_enabled": default.auto_transfer_enabled,
        "zero_disables": disabled.auto_transfer_enabled,
        "negative_disables": negative.auto_transfer_enabled,
        "disabled_never_warns": list(disabled.check_auto_transfer(1000.0, ["u2"])),
        # A window shorter than the warning lead-in clamps to zero, so the
        # very first check warns rather than the warning never firing.
        "short_window_warns_immediately": list(short.check_auto_transfer(0.0, ["u2"])),
        "short_window_transfers": list(TransferManager(auto_transfer_idle_s=5).check_auto_transfer(5.0, ["u2"])),
        "max_queue_length": transfer_module.MAX_QUEUE_LENGTH,
        "warning_lead_s": 10,
    }


def main() -> int:
    """Write the golden corpus and report the case count."""
    corpus = {
        "queues": _record_queues(),
        "auto": _record_auto(),
        "warning_sequence": _record_warning_sequence(),
        "modes": _record_modes(),
        "settings": _record_settings(),
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(QUEUE_CASES)} queue cases, {len(AUTO_CASES)} auto cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
