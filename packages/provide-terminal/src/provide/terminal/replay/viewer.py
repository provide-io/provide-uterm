#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Terminal session replay viewer."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import IO, Any

from provide.telemetry import get_logger

logger = get_logger(__name__)


def _clear_screen(output: IO[str]) -> None:
    print("\x1b[2J\x1b[H", end="", file=output)


def _render_screen(screen: str, output: IO[str]) -> None:
    _clear_screen(output)
    print(screen, end="", file=output)


def _render_frame(
    record: dict[str, Any],
    last_ts: float | None,
    out: IO[str],
    *,
    speed: float,
    step: bool,
) -> float | None:
    """Render one log frame and return the updated last_ts."""
    if last_ts is not None and not step:
        delta = (record.get("ts", last_ts) - last_ts) / min(max(speed, 0.01), 100.0)
        if delta > 0:
            time.sleep(delta)
    _render_screen(record.get("data", {}).get("screen", ""), out)
    if step:
        input("-- next --")
    return record.get("ts", last_ts)


def replay_log(
    log_path: str | Path,
    *,
    speed: float = 1.0,
    step: bool = False,
    events: list[str] | None = None,
    output: IO[str] | None = None,
) -> None:
    """Replay a JSONL session log to the terminal.

    Args:
        log_path: Path to the JSONL session log.
        speed: Playback speed multiplier (1.0 = real time, 2.0 = double speed, max 100.0).
        step: If ``True``, pause between frames waiting for Enter.
        events: Event names to render (default: ``["read", "screen"]``).
        output: File-like object to write to (default: ``sys.stdout``).
    """
    out = output if output is not None else sys.stdout
    log_path = Path(log_path)
    wanted = set(events or ["read", "screen"])
    last_ts: float | None = None

    with log_path.open(encoding="utf-8") as _fh:
        for lineno, line in enumerate(_fh, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("replay_log corrupt line skipped path=%s line=%d", log_path, lineno)
                continue
            if record.get("event") not in wanted:
                continue
            if record.get("data", {}).get("screen") is None:
                continue
            last_ts = _render_frame(record, last_ts, out, speed=speed, step=step)
