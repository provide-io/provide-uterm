#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generic JSONL timeseries storage and I/O."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger

from provide.terminal.manager.constants import EPOCH_TURN_DROP_MIN, EPOCH_TURN_DROP_RATIO

if TYPE_CHECKING:
    from provide.terminal.manager.protocols import TimeseriesPlugin

logger = get_logger(__name__)


class TimeseriesManager:
    """Generic JSONL timeseries recording and retrieval.

    Row building and summary generation are delegated to an optional
    :class:`~provide.terminal.manager.protocols.TimeseriesPlugin`.
    """

    def __init__(
        self,
        get_status: Any,
        *,
        timeseries_dir: str = "logs/metrics",
        interval_s: int = 20,
        plugin: TimeseriesPlugin | None = None,
    ):
        from provide.terminal.manager.constants import TIMESERIES_MAX_BYTES, TIMESERIES_RETENTION_S

        self._get_status = get_status
        self.interval_s = max(1, int(interval_s))
        self.timeseries_dir = Path(timeseries_dir)
        self.timeseries_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.path = self.timeseries_dir / f"swarm_timeseries_{stamp}.jsonl"
        self.samples_count = 0
        self._plugin = plugin
        self._max_bytes = TIMESERIES_MAX_BYTES
        self._fh: Any = None  # persistent file handle for the current epoch
        # Cleanup old timeseries files from previous runs.
        self._cleanup_old(TIMESERIES_RETENTION_S)

    def get_info(self) -> dict[str, Any]:
        """Return timeseries metadata."""
        return {
            "path": str(self.path),
            "interval_seconds": self.interval_s,
            "samples": self.samples_count,
        }

    def get_recent(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return recent timeseries rows."""
        capped = max(1, min(int(limit), 5000))
        rows = self.read_tail(capped)
        return self.trim_to_latest_epoch(rows)

    def get_summary(self, window_minutes: int = 120) -> dict[str, Any]:
        """Build a trailing-window summary.

        Delegates to the plugin if available; otherwise returns basic metadata.
        """
        if self._plugin is not None:
            return self._plugin.get_summary(self, window_minutes)
        return {
            "window_minutes": window_minutes,
            "rows": 0,
            "error": "no timeseries plugin configured",
        }

    def _read_tail_bytes(self, capped: int) -> bytes:
        """Read the raw bytes from the tail of the timeseries file."""
        with self.path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            if pos <= 0:
                return b""
            chunk_size = 64 * 1024
            target_lines = capped + 32
            buf = b""
            newline_count = 0
            while pos > 0 and newline_count < target_lines:
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos, os.SEEK_SET)
                chunk = f.read(read_size)
                if not chunk:  # pragma: no cover — defensive
                    break
                buf = chunk + buf
                newline_count = buf.count(b"\n")
        return buf

    def read_tail(self, limit: int) -> list[dict[str, Any]]:
        """Read up to *limit* most recent JSONL rows efficiently."""
        capped = max(1, int(limit))
        if not self.path.exists():
            return []
        try:
            buf = self._read_tail_bytes(capped)
        except Exception:
            return []

        rows: list[dict[str, Any]] = []
        for raw in buf.splitlines()[-capped:]:
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    @staticmethod
    def trim_to_latest_epoch(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Trim rows to the latest continuous run epoch."""
        if len(rows) <= 1:
            return rows
        latest_epoch_start = 0
        prev_turns = int(rows[0].get("total_turns") or 0)
        prev_agents = int(rows[0].get("total_agents") or 0)
        for idx, row in enumerate(rows[1:], start=1):
            cur_turns = int(row.get("total_turns") or 0)
            cur_agents = int(row.get("total_agents") or 0)
            turn_drop = prev_turns - cur_turns
            drop_threshold = max(EPOCH_TURN_DROP_MIN, int(prev_turns * EPOCH_TURN_DROP_RATIO))
            hard_turn_reset = turn_drop > drop_threshold
            if hard_turn_reset or (prev_agents > 0 and cur_agents == 0):
                latest_epoch_start = idx
            prev_turns = cur_turns
            prev_agents = cur_agents
        return rows[latest_epoch_start:]

    def _build_row(self, status: Any, reason: str) -> dict[str, Any]:
        """Build one timeseries sample row."""
        if self._plugin is not None:
            return self._plugin.build_row(status, reason)
        # Default: basic status snapshot.
        return {
            "ts": time.time(),
            "reason": reason,
            "total_agents": status.total_agents,
            "running": status.running,
            "completed": status.completed,
            "errors": status.errors,
            "stopped": status.stopped,
            "uptime_seconds": status.uptime_seconds,
        }

    def _cleanup_old(self, retention_s: float) -> None:
        """Delete timeseries files older than *retention_s*."""
        cutoff = time.time() - retention_s
        for f in self.timeseries_dir.glob("swarm_timeseries_*.jsonl"):
            if f == self.path:
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    logger.info("timeseries_cleanup", path=str(f))
            except OSError:
                continue

    def _ensure_fh(self) -> Any:
        """Return the persistent file handle, opening it if needed."""
        if self._fh is None or self._fh.closed:
            self._fh = self.path.open("a", encoding="utf-8")
        return self._fh

    def _close_fh(self) -> None:
        """Close the persistent file handle if open."""
        if self._fh is not None and not self._fh.closed:
            self._fh.close()
        self._fh = None

    def _rotate_if_needed(self) -> None:
        """Rotate the current file if it exceeds the size limit."""
        try:
            if self.path.stat().st_size <= self._max_bytes:
                return
        except OSError:
            return
        self._close_fh()  # close before rename
        archived = self.timeseries_dir / f"{self.path.stem}_{self.samples_count}.jsonl"
        self.path.rename(archived)
        logger.info("timeseries_rotated", archived=str(archived))

    def write_sample(self, status: Any, *, reason: str) -> None:
        """Append one timeseries sample row (safe to call from a thread)."""
        try:
            row = self._build_row(status, reason=reason)
            fh = self._ensure_fh()
            fh.write(json.dumps(row, ensure_ascii=True) + "\n")
            fh.flush()
            self._rotate_if_needed()
        except Exception as e:
            self._close_fh()  # reset on error so next write retries open
            logger.exception("failed_to_write_timeseries_sample", error=str(e))

    async def loop(self) -> None:
        """Continuously write timeseries samples."""
        await asyncio.to_thread(self.write_sample, self._get_status(), reason="startup")
        self.samples_count += 1
        while True:
            await asyncio.sleep(self.interval_s)
            await asyncio.to_thread(self.write_sample, self._get_status(), reason="interval")
            self.samples_count += 1
