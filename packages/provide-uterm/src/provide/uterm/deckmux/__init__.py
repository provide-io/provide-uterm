#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""DeckMux — collaborative terminal presence and control transfer."""

from __future__ import annotations

from provide.uterm.deckmux._edge import viewport_to_edge_range
from provide.uterm.deckmux._hub_mixin import DeckMuxMixin
from provide.uterm.deckmux._identity import (
    identity_as_principal,
    parse_identity_frame,
    presence_from_identity,
)
from provide.uterm.deckmux._names import generate_color, generate_name
from provide.uterm.deckmux._presence import PresenceStore, UserPresence
from provide.uterm.deckmux._protocol import (
    MSG_CONTROL_TRANSFER,
    MSG_PRESENCE_LEAVE,
    MSG_PRESENCE_SYNC,
    MSG_PRESENCE_UPDATE,
    MSG_QUEUED_INPUT,
)
from provide.uterm.deckmux._transfer import TransferManager

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
    "identity_as_principal",
    "parse_identity_frame",
    "presence_from_identity",
    "DeckMuxMixin",
    "viewport_to_edge_range",
]
