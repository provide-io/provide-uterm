#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Session registry for the hosted terminal server."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

from provide.terminal.server.models import RecordingConfig, SessionDefinition, SessionRuntimeStatus
from provide.terminal.server.runtime import HostedSessionRuntime

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from provide.terminal.hijack.hub import TermHub


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
    ) -> None:
        self._hub = hub
        self._recording = recording
        self._public_base_url = public_base_url
        self._worker_bearer_token = worker_bearer_token
        self._lock = asyncio.Lock()
        self._sessions: dict[str, SessionDefinition] = {session.session_id: session for session in sessions}
        self._runtimes: dict[str, HostedSessionRuntime] = {}

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
        owner = "server-mode-switch"
        had_hijack = False
        async with self._hub._lock:
            st = self._hub._workers.get(session_id)
            if st is None:
                return False
            if st.hijack_session is not None:
                owner = st.hijack_session.owner
                st.hijack_session = None
                had_hijack = True
            if self._hub._is_dashboard_hijack_active(st):
                st.hijack_owner = None
                st.hijack_owner_expires_at = None
                had_hijack = True
        if not had_hijack:
            return False
        await self._hub._send_worker(
            session_id,
            {"type": "control", "action": "resume", "owner": owner, "lease_s": 0, "ts": time.time()},
        )
        self._hub._notify_hijack_changed(session_id, enabled=False, owner=None)
        await self._hub._broadcast_hijack_state(session_id)
        return True

    async def start_auto_start_sessions(self) -> None:
        for session in list(self._sessions.values()):
            if session.auto_start:
                await self.start_session(session.session_id)

    async def shutdown(self) -> None:
        for runtime in list(self._runtimes.values()):
            await runtime.stop()

    async def list_sessions(self) -> list[SessionRuntimeStatus]:
        async with self._lock:
            sessions = list(self._sessions.values())
        return [self._runtime_for(session).status() for session in sessions]

    async def get_session(self, session_id: str) -> SessionRuntimeStatus:
        async with self._lock:
            session = self._sessions[session_id]
        return self._runtime_for(session).status()

    async def get_definition(self, session_id: str) -> SessionDefinition | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def create_session(self, payload: dict[str, Any]) -> SessionRuntimeStatus:
        session = SessionDefinition(
            session_id=str(payload["session_id"]),
            display_name=str(payload.get("display_name", payload["session_id"])),
            connector_type=str(payload.get("connector_type", "demo")),
            connector_config=dict(payload.get("connector_config", {})),
            input_mode=str(payload.get("input_mode", "open")),  # type: ignore[arg-type]
            auto_start=bool(payload.get("auto_start", False)),
            tags=[str(v) for v in payload.get("tags", [])],
            recording_enabled=(
                None if payload.get("recording_enabled") is None else bool(payload.get("recording_enabled"))
            ),
            owner=(None if payload.get("owner") is None else str(payload.get("owner"))),
            visibility=str(payload.get("visibility", "public")),  # type: ignore[arg-type]
            created_at=time.time(),
            last_active_at=time.time(),
        )
        async with self._lock:
            if session.session_id in self._sessions:
                raise ValueError(f"session already exists: {session.session_id}")
            self._sessions[session.session_id] = session
        runtime = self._runtime_for(session)
        if session.auto_start:
            await runtime.start()
        return runtime.status()

    async def update_session(self, session_id: str, payload: dict[str, Any]) -> SessionRuntimeStatus:
        async with self._lock:
            session = self._sessions[session_id]
            if "display_name" in payload:
                session.display_name = str(payload["display_name"])
            if "input_mode" in payload:
                mode = str(payload["input_mode"])
                if mode in {"open", "hijack"}:
                    session.input_mode = mode  # type: ignore[assignment]
            if "auto_start" in payload:
                session.auto_start = bool(payload["auto_start"])
            if "tags" in payload:
                session.tags = [str(v) for v in payload["tags"]]
            if "recording_enabled" in payload:
                session.recording_enabled = bool(payload["recording_enabled"])
            if "connector_config" in payload:
                session.connector_config = dict(payload["connector_config"])
            session.last_active_at = time.time()
        runtime = self._runtime_for(session)
        if "input_mode" in payload:
            await runtime.set_mode(session.input_mode)
        return runtime.status()

    async def delete_session(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id)
        runtime = self._runtimes.pop(session_id, None)
        if runtime is not None:
            await runtime.stop()

    async def start_session(self, session_id: str) -> SessionRuntimeStatus:
        async with self._lock:
            session = self._sessions[session_id]
            session.last_active_at = time.time()
        runtime = self._runtime_for(session)
        await runtime.start()
        return runtime.status()

    async def stop_session(self, session_id: str) -> SessionRuntimeStatus:
        async with self._lock:
            session = self._sessions[session_id]
            session.last_active_at = time.time()
        runtime = self._runtime_for(session)
        await runtime.stop()
        return runtime.status()

    async def restart_session(self, session_id: str) -> SessionRuntimeStatus:
        async with self._lock:
            session = self._sessions[session_id]
            session.last_active_at = time.time()
        runtime = self._runtime_for(session)
        await runtime.restart()
        return runtime.status()

    async def set_mode(self, session_id: str, mode: str) -> SessionRuntimeStatus:
        async with self._lock:
            session = self._sessions[session_id]
            session.input_mode = mode  # type: ignore[assignment]
            session.last_active_at = time.time()
        if mode == "open":
            await self._force_release_hijack(session_id)
        runtime = self._runtime_for(session)
        await runtime.set_mode(mode)
        return runtime.status()

    async def clear_session(self, session_id: str) -> SessionRuntimeStatus:
        async with self._lock:
            session = self._sessions[session_id]
            session.last_active_at = time.time()
        runtime = self._runtime_for(session)
        await runtime.clear()
        return runtime.status()

    async def analyze_session(self, session_id: str) -> str:
        async with self._lock:
            session = self._sessions[session_id]
            session.last_active_at = time.time()
        return await self._runtime_for(session).analyze()

    async def last_snapshot(self, session_id: str) -> dict[str, Any] | None:
        async with self._hub._lock:
            st = self._hub._workers.get(session_id)
            return None if st is None else st.last_snapshot

    async def events(self, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        async with self._hub._lock:
            st = self._hub._workers.get(session_id)
            if st is None:
                return []
            return list(st.events)[-max(1, min(limit, 500)) :]

    async def recording_meta(self, session_id: str) -> dict[str, Any]:
        async with self._lock:
            session = self._sessions[session_id]
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
            session = self._sessions[session_id]
        return self._runtime_for(session).recording_path

    async def recording_entries(
        self,
        session_id: str,
        *,
        limit: int = 200,
        offset: int | None = None,
        event: str | None = None,
    ) -> list[dict[str, Any]]:
        path = await self.recording_path(session_id)
        if path is None or not path.exists():
            return []
        entries: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        normalized_limit = max(1, min(limit, 500))
        normalized_event = None if event is None else str(event).strip()
        if normalized_event:
            entries = [entry for entry in entries if str(entry.get("event", "")) == normalized_event]
        if offset is None:
            return entries[-normalized_limit:]
        normalized_offset = max(0, offset)
        return entries[normalized_offset : normalized_offset + normalized_limit]
