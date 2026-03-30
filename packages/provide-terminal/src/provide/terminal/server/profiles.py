#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""ConnectionProfile model and FileProfileStore for persisted connection profiles."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from provide.terminal.server.models import ServerBaseModel

if TYPE_CHECKING:
    from pathlib import Path

_MUTABLE_FIELDS = frozenset(
    {
        "name",
        "host",
        "port",
        "username",
        "tags",
        "input_mode",
        "recording_enabled",
        "visibility",
    }
)


class ConnectionProfile(ServerBaseModel):
    """A saved connection target owned by a principal."""

    profile_id: str
    owner: str
    name: str
    connector_type: Literal["ssh", "telnet", "websocket", "ushell", "shell"]
    host: str | None = None
    port: int | None = None
    username: str | None = None
    tags: list[str] = Field(default_factory=list)
    input_mode: Literal["open", "hijack"] = "open"
    recording_enabled: bool = False
    visibility: Literal["private", "shared"] = "private"
    # Profile visibility: "private" = owner-only, "shared" = all authenticated users.
    # Distinct from session Visibility which uses ("public", "operator", "private").
    created_at: float
    updated_at: float


class FileProfileStore:
    """Atomic JSON-file-backed store for connection profiles.

    All writes use a temp-file + Path.replace() for atomicity.
    Concurrent access is serialised with an asyncio.Lock.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._lock = asyncio.Lock()

    def _path(self) -> Path:
        return self._directory / "profiles.json"

    def _read_sync(self) -> list[ConnectionProfile]:
        """Read all profiles from disk. Caller must hold self._lock."""
        path = self._path()
        if not path.exists():
            return []
        try:
            data: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
            return [ConnectionProfile.model_validate(p) for p in data]
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"Profiles store is corrupt at {path}: {exc}") from exc

    def _write_sync(self, profiles: list[ConnectionProfile]) -> None:
        """Write all profiles to disk atomically. Caller must hold self._lock."""
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([p.model_dump(mode="python") for p in profiles], indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)

    async def list_profiles(self, *, owner: str | None = None) -> list[ConnectionProfile]:
        """Return profiles visible to *owner* (own + shared), or all if owner is None."""
        async with self._lock:
            profiles = self._read_sync()
        if owner is None:
            return profiles
        return [p for p in profiles if p.owner == owner or p.visibility == "shared"]

    async def get_profile(self, profile_id: str) -> ConnectionProfile | None:
        """Return the profile with the given ID, or None if not found."""
        async with self._lock:
            profiles = self._read_sync()
        return next((p for p in profiles if p.profile_id == profile_id), None)

    async def create_profile(self, profile: ConnectionProfile) -> ConnectionProfile:
        """Persist a new profile and return it."""
        async with self._lock:
            profiles = self._read_sync()
            profiles.append(profile)
            self._write_sync(profiles)
        return profile

    async def update_profile(self, profile_id: str, updates: dict[str, Any]) -> ConnectionProfile | None:
        """Apply *updates* to the profile and return the updated model, or None if not found."""
        safe_updates = {k: v for k, v in updates.items() if k in _MUTABLE_FIELDS}
        async with self._lock:
            profiles = self._read_sync()
            for i, p in enumerate(profiles):
                if p.profile_id == profile_id:
                    data = p.model_dump(mode="python")
                    data.update(safe_updates)
                    data["updated_at"] = time.time()
                    profiles[i] = ConnectionProfile.model_validate(data)
                    self._write_sync(profiles)
                    return profiles[i]
        return None

    async def delete_profile(self, profile_id: str) -> bool:
        """Delete the profile. Returns True if it existed, False if not found."""
        async with self._lock:
            profiles = self._read_sync()
            new_profiles = [p for p in profiles if p.profile_id != profile_id]
            if len(new_profiles) == len(profiles):
                return False
            self._write_sync(new_profiles)
        return True
