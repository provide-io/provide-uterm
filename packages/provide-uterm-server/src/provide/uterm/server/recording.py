#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from provide.uterm.recording import RecordingStore

if TYPE_CHECKING:
    from pathlib import Path


class WebhookRecordingStore(RecordingStore):
    """Managed implementation of RecordingStore that delegates to a External Management Tier webhook."""

    def __init__(self, url: str, secret: str | None = None, timeout_s: float = 2.0):
        self.url = url
        self.secret = secret
        self.timeout = timeout_s

    async def start_session(self, session_id: str, metadata: dict[str, Any]) -> None:
        await self._post(session_id, "start", {"metadata": metadata})

    async def append_events(self, session_id: str, events: list[dict[str, Any]]) -> None:
        await self._post(session_id, "append", {"events": events})

    async def end_session(self, session_id: str) -> None:
        await self._post(session_id, "end", {})

    async def recording_meta(self, session_id: str) -> dict[str, Any]:
        resp = await self._get(session_id, "meta")
        return resp or {"session_id": session_id, "exists": False, "size_bytes": 0}

    async def get_entries(
        self, session_id: str, limit: int = 200, offset: int | None = None, event: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if offset is not None:
            params["offset"] = offset
        if event is not None:
            params["event"] = event
        resp = await self._get(session_id, "entries", params=params)
        return resp.get("entries", []) if isinstance(resp, dict) else []

    async def get_path(self, session_id: str) -> Path | None:
        _ = session_id
        return None  # No local path for webhook store

    async def _post(self, session_id: str, action: str, payload: dict[str, Any]) -> None:
        data = {"session_id": session_id, "action": action, **payload}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                headers = {"Authorization": f"Bearer {self.secret}"} if self.secret else {}
                await client.post(self.url, json=data, headers=headers)
        except Exception:
            pass  # Best effort for recording

    async def _get(self, session_id: str, action: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.url}/{session_id}/{action}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                headers = {"Authorization": f"Bearer {self.secret}"} if self.secret else {}
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code == 200:
                    return resp.json()
        except Exception:
            return None
