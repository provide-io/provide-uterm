#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""DeckMux — collaborative terminal presence and control transfer."""

from __future__ import annotations

from provide.terminal.deckmux._edge import viewport_to_edge_range
from provide.terminal.deckmux._names import generate_color, generate_name
from provide.terminal.deckmux._presence import PresenceStore, UserPresence
from provide.terminal.deckmux._protocol import (
    MSG_CONTROL_TRANSFER,
    MSG_PRESENCE_LEAVE,
    MSG_PRESENCE_SYNC,
    MSG_PRESENCE_UPDATE,
    MSG_QUEUED_INPUT,
)
from provide.terminal.deckmux._transfer import TransferManager

__all__ = [
    "MSG_CONTROL_TRANSFER",
    "MSG_PRESENCE_LEAVE",
    "MSG_PRESENCE_SYNC",
    "MSG_PRESENCE_UPDATE",
    "MSG_QUEUED_INPUT",
    "PresenceStore",
    "TransferManager",
    "UserPresence",
    "generate_color",
    "generate_name",
    "viewport_to_edge_range",
]
