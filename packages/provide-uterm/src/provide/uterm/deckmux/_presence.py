#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""DeckMux presence state — per-session user tracking."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

# Caps for the untrusted ``selection`` / ``pin`` dicts that a browser ships in a
# presence update. These are stored and re-broadcast verbatim to every joiner,
# so an unbounded value is a memory-amplification / injection surface. A
# legitimate selection is ``{"start": {...}, "end": {...}}`` (a handful of small
# ints) and a pin is a single coordinate, so 2KB of JSON and 16 top-level keys
# is generously above anything legitimate while cheap to enforce.
_MAX_PRESENCE_DICT_BYTES = 2048
_MAX_PRESENCE_DICT_KEYS = 16

# Presence fields whose values come straight from an untrusted browser message
# and are size/shape-validated before being stored.
_VALIDATED_PRESENCE_FIELDS = frozenset({"selection", "pin"})


def _validate_presence_dict(field: str, value: Any) -> None:
    """Validate an untrusted ``selection``/``pin`` value before it is stored.

    ``None`` is allowed (clears the field). Otherwise the value must be a
    ``dict`` whose JSON encoding is at most :data:`_MAX_PRESENCE_DICT_BYTES`
    bytes and which has at most :data:`_MAX_PRESENCE_DICT_KEYS` top-level keys.
    Raises :class:`ValueError` on violation.
    """
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError(f"invalid presence {field}: must be a dict or None")
    if len(value) > _MAX_PRESENCE_DICT_KEYS:
        raise ValueError(f"invalid presence {field}: too many keys ({len(value)} > {_MAX_PRESENCE_DICT_KEYS})")
    encoded_len = len(json.dumps(value, default=str))
    if encoded_len > _MAX_PRESENCE_DICT_BYTES:
        raise ValueError(f"invalid presence {field}: too large ({encoded_len} > {_MAX_PRESENCE_DICT_BYTES} bytes)")


@dataclass
class UserPresence:
    """Ephemeral presence state for a single user in a session."""

    user_id: str
    name: str
    color: str
    role: str
    initials: str = ""
    scroll_line: int = 0
    scroll_range: tuple[int, int] = (0, 0)
    total_lines: int = 0
    selection: dict[str, Any] | None = None
    pin: dict[str, Any] | None = None
    typing: bool = False
    queued_keys: str = ""
    cols: int = 0
    rows: int = 0
    last_activity_at: float = field(default_factory=time.time)
    is_owner: bool = False

    def is_idle(self, threshold_s: float) -> bool:
        """Return True if user has been idle longer than threshold_s."""
        return (time.time() - self.last_activity_at) > threshold_s

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict for JSON transport."""
        return {
            "user_id": self.user_id,
            "name": self.name,
            "color": self.color,
            "role": self.role,
            "initials": self.initials,
            "scroll_line": self.scroll_line,
            "scroll_range": list(self.scroll_range),
            "total_lines": self.total_lines,
            "selection": self.selection,
            "pin": self.pin,
            "typing": self.typing,
            "queued_keys": self.queued_keys,
            "cols": self.cols,
            "rows": self.rows,
            "is_owner": self.is_owner,
        }


class PresenceStore:
    """Per-session ephemeral presence state."""

    def __init__(self) -> None:
        self._users: dict[str, UserPresence] = {}

    def add(self, user_id: str, name: str, color: str, role: str, initials: str = "") -> UserPresence:
        """Add a user to the presence store."""
        p = UserPresence(user_id=user_id, name=name, color=color, role=role, initials=initials)
        self._users[user_id] = p
        return p

    def update(self, user_id: str, **fields: Any) -> UserPresence | None:
        """Update fields on an existing user. Returns None if not found."""
        p = self._users.get(user_id)
        if p is None:
            return None
        # Validate every field up front so a rejected (e.g. oversized)
        # ``selection``/``pin`` leaves the stored user untouched (no partial
        # mutation) before raising.
        for k, v in fields.items():
            if not hasattr(p, k):
                raise ValueError(f"Unknown presence field: {k}")
            if k in _VALIDATED_PRESENCE_FIELDS:
                _validate_presence_dict(k, v)
        for k, v in fields.items():
            setattr(p, k, v)
        p.last_activity_at = time.time()
        return p

    def remove(self, user_id: str) -> UserPresence | None:
        """Remove a user. Returns the removed presence or None."""
        return self._users.pop(user_id, None)

    def get(self, user_id: str) -> UserPresence | None:
        """Get a user's presence by ID."""
        return self._users.get(user_id)

    def get_all(self) -> list[UserPresence]:
        """Get all user presences."""
        return list(self._users.values())

    def get_owner(self) -> UserPresence | None:
        """Get the current owner, if any."""
        for p in self._users.values():
            if p.is_owner:
                return p
        return None

    def set_owner(self, user_id: str) -> None:
        """Set a user as owner (clears previous owner)."""
        for p in self._users.values():
            p.is_owner = p.user_id == user_id

    def clear_owner(self) -> None:
        """Clear the owner flag from all users."""
        for p in self._users.values():
            p.is_owner = False

    def prune_idle(self, threshold_s: float) -> list[str]:
        """Remove users idle longer than threshold_s. Returns removed user_ids."""
        stale = [uid for uid, p in self._users.items() if p.is_idle(threshold_s)]
        for uid in stale:
            self._users.pop(uid)
        return stale

    def get_sync_payload(self, config: dict[str, Any]) -> dict[str, Any]:
        """Build a presence_sync message with all current users."""
        from provide.uterm.deckmux._protocol import make_presence_sync

        return make_presence_sync([p.to_dict() for p in self._users.values()], config)

    def taken_colors(self) -> frozenset[str]:
        """Return the set of colors currently in use."""
        return frozenset(p.color for p in self._users.values())

    @property
    def count(self) -> int:
        """Number of users in the store."""
        return len(self._users)
