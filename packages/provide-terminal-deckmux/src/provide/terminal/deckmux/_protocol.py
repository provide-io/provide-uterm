#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""DeckMux protocol — message types and serialization."""

from __future__ import annotations

from typing import Any, Literal

MSG_PRESENCE_UPDATE = "presence_update"
MSG_PRESENCE_SYNC = "presence_sync"
MSG_PRESENCE_LEAVE = "presence_leave"
MSG_CONTROL_TRANSFER = "control_transfer"
MSG_QUEUED_INPUT = "queued_input"
MSG_CONTROL_REQUEST = "control_request"
MSG_AUTO_TRANSFER_WARNING = "auto_transfer_warning"

TransferReason = Literal["handover", "auto_idle", "admin_takeover", "lease_expired"]
KeystrokeQueueMode = Literal["display", "replay"]

# UTF-8 key symbols for keystroke queue display
KEY_SYMBOLS: dict[str, str] = {
    "\x1b[A": "↑",
    "\x1b[B": "↓",
    "\x1b[C": "→",
    "\x1b[D": "←",
    "\r": "↵",
    "\n": "↵",
    "\t": "⇥",
    "\x7f": "⌫",
    "\x08": "⌫",
    "\x1b": "⎋",
}


def encode_keys_display(raw_keys: str) -> str:
    """Convert raw keystroke bytes to UTF-8 display symbols."""
    result = []
    i = 0
    while i < len(raw_keys):
        # Check for escape sequences (3 chars)
        if i + 2 < len(raw_keys) and raw_keys[i : i + 3] in KEY_SYMBOLS:
            result.append(KEY_SYMBOLS[raw_keys[i : i + 3]])
            i += 3
        elif raw_keys[i] in KEY_SYMBOLS:
            result.append(KEY_SYMBOLS[raw_keys[i]])
            i += 1
        elif raw_keys[i] >= " ":  # printable
            result.append(raw_keys[i])
            i += 1
        else:
            i += 1  # skip non-printable
    return "".join(result)


def make_presence_update(user_id: str, name: str, color: str, role: str, **fields: Any) -> dict[str, Any]:
    """Build a presence_update message for broadcast."""
    msg: dict[str, Any] = {
        "type": MSG_PRESENCE_UPDATE,
        "user_id": user_id,
        "name": name,
        "color": color,
        "role": role,
    }
    for k in ("scroll_line", "scroll_range", "selection", "pin", "typing", "queued_keys", "is_owner"):
        if k in fields:
            msg[k] = fields[k]
    return msg


def make_presence_sync(users: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    """Build a presence_sync message."""
    return {"type": MSG_PRESENCE_SYNC, "users": users, "config": config}


def make_presence_leave(user_id: str) -> dict[str, Any]:
    """Build a presence_leave message."""
    return {"type": MSG_PRESENCE_LEAVE, "user_id": user_id}


def make_control_transfer(
    from_user: str, to_user: str, reason: TransferReason, queued_keys: str = ""
) -> dict[str, Any]:
    """Build a control_transfer message."""
    return {
        "type": MSG_CONTROL_TRANSFER,
        "from_user_id": from_user,
        "to_user_id": to_user,
        "reason": reason,
        "queued_keys": queued_keys,
    }
