#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Pydantic models and internal dataclasses for the terminal hijack hub."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

try:
    from fastapi import WebSocket  # noqa: TC002
    from pydantic import BaseModel
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for hijack hub/routes: pip install 'provide-terminal[websocket]'") from _e

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Internal state (dataclasses — no serialisation overhead needed)
# ---------------------------------------------------------------------------


@dataclass
class HijackSession:
    """A live REST hijack lease."""

    hijack_id: str
    owner: str
    acquired_at: float
    lease_expires_at: float
    last_heartbeat: float


@dataclass
class BotTermState:
    """Per-bot connection state held by :class:`~provide.terminal.hijack.hub.TermHub`."""

    worker_ws: WebSocket | None = None
    browsers: set[WebSocket] = field(default_factory=set)
    hijack_owner: WebSocket | None = None  # dashboard WS that holds the lease
    hijack_owner_expires_at: float | None = None
    hijack_session: HijackSession | None = None  # REST lease
    last_snapshot: dict[str, Any] | None = None
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=2000))
    event_seq: int = 0


# ---------------------------------------------------------------------------
# API request models (pydantic — used by FastAPI for request validation)
# ---------------------------------------------------------------------------


class HijackAcquireRequest(BaseModel):
    owner: str = "mcp"
    lease_s: int = 90


class HijackHeartbeatRequest(BaseModel):
    lease_s: int = 90


class HijackSendRequest(BaseModel):
    keys: str
    expect_prompt_id: str | None = None
    expect_regex: str | None = None
    timeout_ms: int = 2000
    poll_interval_ms: int = 120


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def extract_prompt_id(snapshot: dict[str, Any] | None) -> str | None:
    """Pull ``prompt_id`` out of a snapshot dict (returns ``None`` if absent)."""
    if not snapshot:
        return None
    prompt = snapshot.get("prompt_detected")
    if isinstance(prompt, dict):
        value = prompt.get("prompt_id")
        if isinstance(value, str) and value:
            return value
    return None
