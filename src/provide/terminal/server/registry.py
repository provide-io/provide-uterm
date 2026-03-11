#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Session registry for the hosted terminal server."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections import deque
from typing import TYPE_CHECKING, Any

from provide.terminal.server.connectors import KNOWN_CONNECTOR_TYPES
from provide.terminal.server.models import RecordingConfig, SessionDefinition, SessionRuntimeStatus
from provide.terminal.server.runtime import HostedSessionRuntime

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from provide.terminal.hijack.hub import TermHub


class SessionValidationError(ValueError):
    """Raised when session creation/update data fails format validation."""


class SessionRegistry:
    """Config-backed registry for named hosted sessions and their runtimes."""

    def __init__(
        self,
        sessions: Iterable[SessionDefinition],
        *,
        hub: TermHub,
        public_base_url: str,
        recording: RecordingConfig,
        worker_bearer_token: str | None = None,
        max_sessions: int | None = None,
    ) -> None:
        self._hub = hub
        self._recording = recording
        self._public_base_url = public_base_url
        self._worker_bearer_token = worker_bearer_token
        self._max_sessions = max_sessions
        self._lock = asyncio.Lock()
        self._sessions: dict[str, SessionDefinition] = {session.session_id: session for session in sessions}
        self._runtimes: dict[str, HostedSessionRuntime] = {}
        hub.on_worker_empty = self._on_worker_empty

    async def _on_worker_empty(self, session_id: str) -> None:
        """Auto-delete an ephemeral session when the last browser disconnects.

        A short grace period lets reconnecting browsers (page refresh, brief
        network drop, or the initial redirect to a freshly-created session) land
        before the session is torn down.
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None or not session.ephemeral:
                return
        await asyncio.sleep(5)
        if await self._hub.browser_count(session_id) > 0:
            return
        with contextlib.suppress(KeyError):
            await self.delete_session(session_id)

    def _require_session(self, session_id: str) -> SessionDefinition:
        """Return the session definition or raise ``KeyError``.  Caller must hold ``self._lock``."""
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"unknown session: {session_id!r}")
        return session

    def _runtime_for(self, session: SessionDefinition) -> HostedSessionRuntime:
        runtime = self._runtimes.get(session.session_id)
        if runtime is None:
            runtime = HostedSessionRuntime(
                session,
                public_base_url=self._public_base_url,
                recording=self._recording,
                worker_bearer_token=self._worker_bearer_token,
            )
            self._runtimes[session.session_id] = runtime
        return runtime

    async def _force_release_hijack(self, session_id: str) -> bool:
        return await self._hub.force_release_hijack(session_id)

    async def start_auto_start_sessions(self) -> None:
        for session in list(self._sessions.values()):
            if session.auto_start:
                await self.start_session(session.session_id)

    async def shutdown(self) -> None:
        for runtime in list(self._runtimes.values()):
            await runtime.stop()

    async def list_sessions(self) -> list[SessionRuntimeStatus]:
        async with self._lock:
            runtimes = [self._runtime_for(s) for s in self._sessions.values()]
        return [r.status() for r in runtimes]

    async def list_sessions_with_definitions(self) -> list[tuple[SessionRuntimeStatus, SessionDefinition]]:
        """Return (status, definition) pairs in a single lock acquisition."""
        async with self._lock:
            pairs = [(self._runtime_for(s), s) for s in self._sessions.values()]
        return [(r.status(), s) for r, s in pairs]

    async def get_session(self, session_id: str) -> SessionRuntimeStatus:
        async with self._lock:
            session = self._require_session(session_id)
            runtime = self._runtime_for(session)
        return runtime.status()

    async def get_definition(self, session_id: str) -> SessionDefinition | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def create_session(self, payload: dict[str, Any]) -> SessionRuntimeStatus:
        session_id = str(payload["session_id"])
        if not re.match(r"^[\w\-]+$", session_id):
            raise SessionValidationError(f"session_id must match ^[\\w\\-]+$, got: {session_id!r}")
        connector_type_raw = str(payload.get("connector_type", "shell"))
        if connector_type_raw not in KNOWN_CONNECTOR_TYPES:
            raise SessionValidationError(
                f"connector_type must be one of {sorted(KNOWN_CONNECTOR_TYPES)}, got: {connector_type_raw!r}"
            )
        input_mode_raw = str(payload.get("input_mode", "open"))
        if input_mode_raw not in {"open", "hijack"}:
            raise SessionValidationError(f"input_mode must be 'open' or 'hijack', got: {input_mode_raw!r}")
        visibility_raw = str(payload.get("visibility", "public"))
        if visibility_raw not in {"public", "operator", "private"}:
            raise SessionValidationError(
                f"visibility must be 'public', 'operator', or 'private', got: {visibility_raw!r}"
            )
        session = SessionDefinition(
            session_id=session_id,
            display_name=str(payload.get("display_name", session_id)),
            connector_type=connector_type_raw,
            connector_config=dict(payload.get("connector_config", {})),
            input_mode=input_mode_raw,  # type: ignore[arg-type]
            auto_start=bool(payload.get("auto_start", False)),
            tags=[str(v) for v in payload.get("tags", [])],
            recording_enabled=(
                None if payload.get("recording_enabled") is None else bool(payload.get("recording_enabled"))
            ),
            owner=(None if payload.get("owner") is None else str(payload.get("owner"))),
            visibility=visibility_raw,  # type: ignore[arg-type]
            ephemeral=bool(payload.get("ephemeral", False)),
        )
        async with self._lock:
            if self._max_sessions is not None and len(self._sessions) >= self._max_sessions:
                raise ValueError(f"session limit reached: max_sessions={self._max_sessions}")
            if session.session_id in self._sessions:
                raise ValueError(f"session already exists: {session.session_id}")
            self._sessions[session.session_id] = session
            runtime = self._runtime_for(session)
        if session.auto_start:
            await runtime.start()
        return runtime.status()

    async def update_session(self, session_id: str, payload: dict[str, Any]) -> SessionRuntimeStatus:
        async with self._lock:
            session = self._require_session(session_id)
            if "display_name" in payload:
                session.display_name = str(payload["display_name"])
            if "input_mode" in payload:
                mode = str(payload["input_mode"])
                if mode not in {"open", "hijack"}:
                    raise SessionValidationError(f"input_mode must be 'open' or 'hijack', got: {mode!r}")
                session.input_mode = mode  # type: ignore[assignment]
            if "visibility" in payload:
                vis = str(payload["visibility"])
                if vis not in {"public", "operator", "private"}:
                    raise SessionValidationError(f"visibility must be 'public', 'operator', or 'private', got: {vis!r}")
                session.visibility = vis  # type: ignore[assignment]
            if "auto_start" in payload:
                session.auto_start = bool(payload["auto_start"])
            if "tags" in payload:
                session.tags = [str(v) for v in payload["tags"]]
            if "recording_enabled" in payload:
                session.recording_enabled = bool(payload["recording_enabled"])
            if "connector_config" in payload:
                session.connector_config = dict(payload["connector_config"])
            runtime = self._runtime_for(session)
        if "input_mode" in payload:
            await runtime.set_mode(session.input_mode)
        return runtime.status()

    async def delete_session(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)
            runtime = self._runtimes.pop(session_id, None)
        if runtime is not None:
            await runtime.stop()

    async def start_session(self, session_id: str) -> SessionRuntimeStatus:
        async with self._lock:
            session = self._require_session(session_id)
            runtime = self._runtime_for(session)
        await runtime.start()
        return runtime.status()

    async def stop_session(self, session_id: str) -> SessionRuntimeStatus:
        async with self._lock:
            session = self._require_session(session_id)
            runtime = self._runtime_for(session)
        await runtime.stop()
        return runtime.status()

    async def restart_session(self, session_id: str) -> SessionRuntimeStatus:
        async with self._lock:
            session = self._require_session(session_id)
            runtime = self._runtime_for(session)
        await runtime.restart()
        return runtime.status()

    async def set_mode(self, session_id: str, mode: str) -> SessionRuntimeStatus:
        async with self._lock:
            session = self._require_session(session_id)
            session.input_mode = mode  # type: ignore[assignment]
            runtime = self._runtime_for(session)
        if mode == "open":
            await self._force_release_hijack(session_id)
        await runtime.set_mode(mode)
        return runtime.status()

    async def clear_session(self, session_id: str) -> SessionRuntimeStatus:
        async with self._lock:
            session = self._require_session(session_id)
            runtime = self._runtime_for(session)
        await runtime.clear()
        return runtime.status()

    async def analyze_session(self, session_id: str) -> str:
        async with self._lock:
            session = self._require_session(session_id)
            runtime = self._runtime_for(session)
        return await runtime.analyze()

    async def last_snapshot(self, session_id: str) -> dict[str, Any] | None:
        return await self._hub.get_last_snapshot(session_id)

    async def events(self, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        return await self._hub.get_recent_events(session_id, limit)

    async def recording_meta(self, session_id: str) -> dict[str, Any]:
        async with self._lock:
            session = self._require_session(session_id)
            runtime = self._runtime_for(session)
        path = runtime.recording_path
        return {
            "session_id": session_id,
            "enabled": runtime.status().recording_enabled,
            "path": (str(path) if path is not None else None),
            "exists": bool(path and path.exists()),
        }

    async def recording_path(self, session_id: str) -> Path | None:
        async with self._lock:
            session = self._require_session(session_id)
            runtime = self._runtime_for(session)
        return runtime.recording_path

    async def recording_entries(
        self,
        session_id: str,
        *,
        limit: int = 200,
        offset: int | None = None,
        event: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recording entries for *session_id*.

        Offset semantics:
        - ``offset=None`` (default): return the **last** *limit* entries (tail).
        - ``offset=0`` or positive integer: skip that many accepted entries from
          the beginning of the file, then return up to *limit* (head + skip).
        """
        path = await self.recording_path(session_id)
        if path is None or not path.exists():
            return []
        normalized_limit = max(1, min(limit, 500))
        normalized_event = None if event is None else str(event).strip()
        normalized_offset = max(0, offset) if offset is not None else None

        # Stream line-by-line to avoid loading the entire file into memory.
        # When offset is given, skip accepted entries until the offset is reached,
        # then collect up to limit — avoiding O(N) in-memory accumulation.
        def _read_with_offset(offset: int) -> list[dict[str, Any]]:
            entries: list[dict[str, Any]] = []
            skipped = 0
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if normalized_event and str(entry.get("event", "")) != normalized_event:
                        continue
                    if skipped < offset:
                        skipped += 1
                        continue
                    entries.append(entry)
                    if len(entries) >= normalized_limit:
                        break
            return entries

        def _read_tail() -> list[dict[str, Any]]:
            tail: deque[dict[str, Any]] = deque(maxlen=normalized_limit)
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if normalized_event and str(entry.get("event", "")) != normalized_event:
                        continue
                    tail.append(entry)
            return list(tail)

        if normalized_offset is not None:
            return await asyncio.to_thread(_read_with_offset, normalized_offset)
        return await asyncio.to_thread(_read_tail)
