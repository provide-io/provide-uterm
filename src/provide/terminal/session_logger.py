#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""JSONL session logger for recording BBS sessions."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from io import TextIOWrapper


class SessionLogger:
    """Async JSONL session recorder.

    Each log entry is a JSON object on its own line with at minimum:
    ``{"ts": ..., "event": ..., "data": {...}}``.

    Usage::

        logger = SessionLogger("/tmp/session.jsonl")
        await logger.start(session_id=42)
        await logger.log_send("hello")
        await logger.stop()
    """

    def __init__(self, log_path: str | Path) -> None:
        self._log_path = Path(log_path)
        self._file: TextIOWrapper | None = None
        self._lock = asyncio.Lock()
        self._session_id: int | None = None
        self._context: dict[str, str] = {}

    async def start(self, session_id: int) -> None:
        """Open log file and write a ``log_start`` header entry."""
        async with self._lock:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self._log_path.open("a", encoding="utf-8")
            self._session_id = session_id
            self._write_event_unlocked("log_start", {"path": str(self._log_path), "started_at": time.time()})
            file_to_flush = self._file
        await self._flush(file_to_flush)

    async def stop(self) -> None:
        """Write a ``log_stop`` entry and close the file."""
        file_to_close: TextIOWrapper | None = None
        async with self._lock:
            if self._file:
                self._write_event_unlocked("log_stop", {})
                file_to_close = self._file
                self._file = None
        if file_to_close is not None:
            # Flush and close outside the lock so concurrent writers are not
            # blocked during the I/O syscalls.  Both operations are offloaded to
            # a thread-pool executor so the event loop is not stalled by kernel
            # write-back flushing (which can block close() on some filesystems).
            loop = asyncio.get_running_loop()
            with contextlib.suppress(OSError):
                await loop.run_in_executor(None, file_to_close.flush)
            with contextlib.suppress(OSError):
                await loop.run_in_executor(None, file_to_close.close)

    async def log_send(self, keys: str) -> None:
        """Log sent keystrokes."""
        payload = keys.encode("cp437", errors="replace")
        await self._write_event("send", {"keys": keys, "bytes_b64": base64.b64encode(payload).decode("ascii")})

    async def log_send_masked(self, byte_count: int) -> None:
        """Log a credential send without capturing the actual value."""
        await self._write_event(
            "send",
            {
                "keys": "***",
                "bytes_b64": base64.b64encode(b"***").decode("ascii"),
                "masked": True,
                "byte_count": byte_count,
            },
        )

    async def log_screen(self, snapshot: dict[str, Any], raw: bytes) -> None:
        """Log a screen snapshot with raw bytes."""
        data = {
            **snapshot,
            "raw": raw.decode("cp437", errors="replace"),
            "raw_bytes_b64": base64.b64encode(raw).decode("ascii"),
        }
        await self._write_event("read", data)

    async def log_event(self, event: str, data: dict[str, Any]) -> None:
        """Log an arbitrary named event."""
        await self._write_event(event, data)

    def set_context(self, context: dict[str, str]) -> None:
        """Set metadata context for subsequent log entries."""
        self._context = {str(k): str(v) for k, v in context.items()}

    def clear_context(self) -> None:
        """Clear metadata context."""
        self._context = {}

    async def _write_event(self, event: str, data: dict[str, Any]) -> None:
        # Write synchronously under the lock, then flush outside to avoid
        # holding the lock across a thread-pool await.
        file_to_flush: TextIOWrapper | None = None
        async with self._lock:
            self._write_event_unlocked(event, data)
            file_to_flush = self._file
        await self._flush(file_to_flush)

    def _write_event_unlocked(self, event: str, data: dict[str, Any]) -> None:
        """Synchronously write one record.  Caller **must** hold ``self._lock``."""
        if not self._file:
            return
        record: dict[str, Any] = {"ts": time.time(), "event": event, "data": data}
        if self._session_id is not None:
            record["session_id"] = self._session_id
        if self._context:
            ctx = dict(self._context)
            record["ctx"] = ctx
            if "menu" in ctx:
                record["menu"] = ctx["menu"]
            if "action" in ctx:
                record["action"] = ctx["action"]
        self._file.write(json.dumps(record, ensure_ascii=True) + "\n")

    @staticmethod
    async def _flush(file: TextIOWrapper | None) -> None:
        """Flush *file* in a thread-pool executor (no-op if *file* is None)."""
        if file is None:
            return
        loop = asyncio.get_running_loop()
        with contextlib.suppress(OSError):
            await loop.run_in_executor(None, file.flush)
