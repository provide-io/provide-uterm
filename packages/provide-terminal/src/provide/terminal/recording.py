#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import json
import time
import asyncio
from collections import deque
from pathlib import Path
from typing import Any, Protocol, runtime_checkable, TYPE_CHECKING

if TYPE_CHECKING:
    from io import TextIOWrapper

@runtime_checkable
class RecordingStore(Protocol):
    """Protocol for persisting and retrieving session recordings."""

    async def start_session(self, session_id: str, metadata: dict[str, Any]) -> None:
        """Initialize a new recording session."""
        ...

    async def append_events(self, session_id: str, events: list[dict[str, Any]]) -> None:
        """Append a batch of events to the session recording."""
        ...

    async def end_session(self, session_id: str) -> None:
        """Finalize the recording session."""
        ...

    async def recording_meta(self, session_id: str) -> dict[str, Any]:
        """Retrieve metadata about the recording (exists, enabled, etc.)."""
        ...

    async def get_entries(
        self, 
        session_id: str, 
        limit: int = 200, 
        offset: int | None = None, 
        event: str | None = None
    ) -> list[dict[str, Any]]:
        """Retrieve paginated events from the recording."""
        ...

    async def get_path(self, session_id: str) -> Path | None:
        """Return the local file path if available (for legacy downloads)."""
        ...


class LocalFileRecordingStore(RecordingStore):
    """File-backed implementation of RecordingStore using JSONL files."""

    def __init__(self, directory: str | Path):
        self._directory = Path(directory)
        self._locks: dict[str, asyncio.Lock] = {}
        self._files: dict[str, TextIOWrapper] = {}

    def _get_path(self, session_id: str) -> Path:
        return self._directory / f"{session_id}.jsonl"

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    async def start_session(self, session_id: str, metadata: dict[str, Any]) -> None:
        async with self._get_lock(session_id):
            path = self._get_path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            f = path.open("a", encoding="utf-8")
            self._files[session_id] = f
            event = {"ts": time.time(), "event": "log_start", "data": metadata, "session_id": session_id}
            f.write(json.dumps(event) + "\n")
            f.flush()

    async def append_events(self, session_id: str, events: list[dict[str, Any]]) -> None:
        async with self._get_lock(session_id):
            f = self._files.get(session_id)
            if not f:
                path = self._get_path(session_id)
                f = path.open("a", encoding="utf-8")
                self._files[session_id] = f
            
            for event in events:
                f.write(json.dumps(event) + "\n")
            f.flush()

    async def end_session(self, session_id: str) -> None:
        async with self._get_lock(session_id):
            f = self._files.pop(session_id, None)
            if f:
                event = {"ts": time.time(), "event": "log_stop", "data": {}, "session_id": session_id}
                f.write(json.dumps(event) + "\n")
                f.close()

    async def recording_meta(self, session_id: str) -> dict[str, Any]:
        path = self._get_path(session_id)
        return {
            "session_id": session_id,
            "exists": path.exists(),
            "path": str(path) if path.exists() else None,
            "size_bytes": path.stat().st_size if path.exists() else 0,
        }

    async def get_entries(
        self, 
        session_id: str, 
        limit: int = 200, 
        offset: int | None = None, 
        event: str | None = None
    ) -> list[dict[str, Any]]:
        path = self._get_path(session_id)
        if not path.exists():
            return []
        
        normalized_limit = max(1, min(limit, 500))
        
        def _read():
            if offset is not None:
                entries = []
                skipped = 0
                with path.open(encoding="utf-8") as f:
                    for line in f:
                        try:
                            item = json.loads(line)
                            if event and item.get("event") != event: continue
                            if skipped < offset:
                                skipped += 1
                                continue
                            entries.append(item)
                            if len(entries) >= normalized_limit: break
                        except json.JSONDecodeError: continue
                return entries
            else:
                tail = deque(maxlen=normalized_limit)
                with path.open(encoding="utf-8") as f:
                    for line in f:
                        try:
                            item = json.loads(line)
                            if event and item.get("event") != event: continue
                            tail.append(item)
                        except json.JSONDecodeError: continue
                return list(tail)

        return await asyncio.to_thread(_read)

    async def get_path(self, session_id: str) -> Path | None:
        path = self._get_path(session_id)
        return path if path.exists() else None
