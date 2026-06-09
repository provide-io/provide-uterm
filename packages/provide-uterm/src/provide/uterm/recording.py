#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Recording-store backends for terminal session capture.

Defines the :class:`RecordingStore` protocol plus three reference
implementations: :class:`LocalFileRecordingStore` (JSONL files),
:class:`InMemoryRecordingStore` (ephemeral), and :class:`NullRecordingStore`
(no-op). See the protocol docstring for the lifecycle contract.
"""

from __future__ import annotations

import asyncio
import json
import os  # noqa: F401  # re-exported for tests that monkeypatch syscall helpers
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from provide.uterm.file_io import secure_open_append

if TYPE_CHECKING:
    from io import TextIOWrapper


def _ensure_owner_only_dir(directory: Path) -> None:
    """Create ``directory`` (and parents) 0o700, re-tightening if it pre-exists.

    L13: ``mkdir(mode=0o700, exist_ok=True)`` only applies the mode when it
    *creates* the directory — a pre-existing 0o755 recordings dir would stay
    group/world-readable (session filenames enumerable by other local users).
    The explicit ``chmod`` after the mkdir enforces 0o700 whether or not the
    directory already existed.
    """
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)


def _open_append_owner_only(path: Path) -> TextIOWrapper:
    """Open ``path`` for append, symlink-safe, with owner-only (0o600) perms.

    Creating the file via ``os.open(..., 0o600)`` and wrapping the fd avoids the
    TOCTOU window of ``open()`` then ``chmod()`` (where the file is briefly
    world/group-readable). The mode only applies on creation and is subject to
    umask — but umask can only *remove* bits, and 0o600 has no group/world bits
    for umask to clear, so the result is always exactly 0o600.

    L12 hardening:

    * ``O_NOFOLLOW`` refuses to open a symlink at the target path — an attacker
      who pre-creates a symlink there cannot redirect our writes through to the
      link target. The open raises ``OSError`` (ELOOP) instead; a symlinked
      recording path is an attack/misconfig, so failing loudly is correct.
    * The create-mode arg only applies on *creation*, so a PRE-EXISTING loose
      file would keep its mode (e.g. a world-readable 0o644 file). ``fchmod`` on
      the just-opened fd re-tightens it to 0o600 with no TOCTOU race (we target
      the fd, not the path).
    * A regular-file check (``S_ISREG``) rejects a fifo/device pre-created at the
      path — there is no legitimate non-regular recording sink, and refusing
      closes a residual write-blocking / observation vector.
    """
    return secure_open_append(path)


@runtime_checkable
class RecordingStore(Protocol):
    """Protocol for persisting and retrieving session recordings.

    Implement this protocol to provide a custom recording backend (e.g. S3,
    GCS, a database, or any remote object store).  The lifecycle is:

    1. ``start_session`` -- called once when a session begins recording.
       Persist *metadata* keyed by *session_id*.
    2. ``append_events`` -- called repeatedly with batches of JSON-serialisable
       event dicts.  Each dict has at minimum ``ts``, ``event``, and ``data``.
    3. ``end_session`` -- called once when the session stops recording.

    Query methods (``recording_meta``, ``get_entries``, ``get_path``) may be
    called at any time, including while the session is still active.

    See ``InMemoryRecordingStore`` for a minimal reference implementation.
    """

    async def start_session(self, session_id: str, metadata: dict[str, Any]) -> None:
        """Initialize a new recording session.

        Args:
            session_id: Unique identifier for the session.
            metadata: Arbitrary key/value pairs describing the session.
        """
        ...

    async def append_events(self, session_id: str, events: list[dict[str, Any]]) -> None:
        """Append a batch of events to the session recording.

        Args:
            session_id: The session to append to.
            events: One or more event dicts to persist (order matters).
        """
        ...

    async def end_session(self, session_id: str) -> None:
        """Finalize the recording session.

        After this call the session is considered closed.  Implementations
        should flush any buffers and release resources.

        Args:
            session_id: The session to finalize.
        """
        ...

    async def recording_meta(self, session_id: str) -> dict[str, Any]:
        """Retrieve metadata about the recording.

        Must return at least ``{"session_id": ..., "exists": bool,
        "size_bytes": int}``.  Additional keys are permitted.

        Args:
            session_id: The session to query.
        """
        ...

    async def get_entries(
        self,
        session_id: str,
        limit: int = 200,  # pragma: no mutate — trampoline-masked default
        offset: int | None = None,
        event: str | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve paginated events from the recording.

        When *offset* is ``None``, return the **last** *limit* events (tail).
        When *offset* is given, skip that many matching events, then return up
        to *limit*.  If *event* is provided, only include entries whose
        ``"event"`` key matches.

        Args:
            session_id: The session to read from.
            limit: Maximum number of events to return (clamped 1..500).
            offset: Number of matching events to skip from the start.
            event: Optional event-type filter.
        """
        ...

    async def get_path(self, session_id: str) -> Path | None:
        """Return a local file path for the recording, if one exists.

        Remote stores that have no local file should return ``None``.

        Args:
            session_id: The session to locate.
        """
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
            _ensure_owner_only_dir(path.parent)
            f = _open_append_owner_only(path)
            self._files[session_id] = f
            event = {"ts": time.time(), "event": "log_start", "data": metadata, "session_id": session_id}
            f.write(json.dumps(event) + "\n")
            f.flush()

    async def append_events(self, session_id: str, events: list[dict[str, Any]]) -> None:
        async with self._get_lock(session_id):
            f = self._files.get(session_id)
            if not f:
                path = self._get_path(session_id)
                _ensure_owner_only_dir(path.parent)
                f = _open_append_owner_only(path)
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
        limit: int = 200,  # pragma: no mutate — trampoline-masked default
        offset: int | None = None,
        event: str | None = None,
    ) -> list[dict[str, Any]]:
        path = self._get_path(session_id)
        if not path.exists():
            return []

        normalized_limit = max(1, min(limit, 500))

        def _read() -> list[dict[str, Any]]:
            if offset is not None:
                entries: list[dict[str, Any]] = []
                skipped = 0
                with path.open(encoding="utf-8") as f:
                    for line in f:
                        try:
                            item = json.loads(line)
                            if event and item.get("event") != event:
                                continue
                            if skipped < offset:
                                skipped += 1
                                continue
                            entries.append(item)
                            if len(entries) >= normalized_limit:
                                break
                        except json.JSONDecodeError:
                            continue
                return entries
            tail: deque[dict[str, Any]] = deque(maxlen=normalized_limit)
            with path.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        item = json.loads(line)
                        if event and item.get("event") != event:
                            continue
                        tail.append(item)
                    except json.JSONDecodeError:
                        continue
            return list(tail)

        return await asyncio.to_thread(_read)

    async def get_path(self, session_id: str) -> Path | None:
        path = self._get_path(session_id)
        return path if path.exists() else None


class InMemoryRecordingStore:
    """In-memory implementation of ``RecordingStore``.

    Keeps all events in Python lists.  Useful for tests and as a reference
    implementation showing the expected behaviour for custom remote stores.

    Note: data is lost when the process exits.  Do not use in production
    unless you only need ephemeral recordings.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}

    async def start_session(self, session_id: str, metadata: dict[str, Any]) -> None:
        self._sessions[session_id] = {"metadata": metadata, "active": True}
        self._events.setdefault(session_id, [])
        start_event = {"ts": time.time(), "event": "log_start", "data": metadata, "session_id": session_id}
        self._events[session_id].append(start_event)

    async def append_events(self, session_id: str, events: list[dict[str, Any]]) -> None:
        self._events.setdefault(session_id, []).extend(events)

    async def end_session(self, session_id: str) -> None:
        stop_event = {"ts": time.time(), "event": "log_stop", "data": {}, "session_id": session_id}
        self._events.setdefault(session_id, []).append(stop_event)
        if session_id in self._sessions:
            self._sessions[session_id]["active"] = False

    async def recording_meta(self, session_id: str) -> dict[str, Any]:
        exists = session_id in self._events and len(self._events[session_id]) > 0
        size_bytes = sum(len(json.dumps(e)) + 1 for e in self._events.get(session_id, []))
        return {"session_id": session_id, "exists": exists, "size_bytes": size_bytes}

    async def get_entries(
        self,
        session_id: str,
        limit: int = 200,  # pragma: no mutate — trampoline-masked default
        offset: int | None = None,
        event: str | None = None,
    ) -> list[dict[str, Any]]:
        all_events = self._events.get(session_id, [])
        if event is not None:
            all_events = [e for e in all_events if e.get("event") == event]

        normalized_limit = max(1, min(limit, 500))

        if offset is not None:
            return all_events[offset : offset + normalized_limit]
        # Tail behaviour: return last N events
        return all_events[-normalized_limit:]

    async def get_path(self, _session_id: str) -> Path | None:
        return None


class NullRecordingStore:
    """No-op implementation of ``RecordingStore``.

    All writes are silently discarded and all reads return empty results.
    Use this when recording is disabled -- it eliminates ``None`` checks
    throughout the calling code while keeping the ``RecordingStore`` interface
    consistent.
    """

    async def start_session(self, _session_id: str, _metadata: dict[str, Any]) -> None:
        return

    async def append_events(self, _session_id: str, _events: list[dict[str, Any]]) -> None:
        return

    async def end_session(self, _session_id: str) -> None:
        return

    async def recording_meta(self, session_id: str) -> dict[str, Any]:
        return {"session_id": session_id, "exists": False, "size_bytes": 0}

    async def get_entries(
        self,
        _session_id: str,
        limit: int = 200,  # pragma: no mutate — trampoline-masked default
        offset: int | None = None,
        event: str | None = None,
    ) -> list[dict[str, Any]]:
        return []

    async def get_path(self, _session_id: str) -> Path | None:
        return None
