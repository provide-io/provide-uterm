#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Pydantic models and internal dataclasses for the terminal hijack hub."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from provide.terminal.hijack.rest_helpers import MAX_EXPECT_REGEX_LEN


def _safe_int(val: Any, default: int, *, min_val: int | None = None) -> int:
    """Coerce *val* to ``int``, returning *default* on failure, ``None``, or out-of-range."""
    try:
        result = int(default if val is None else val)
    except (ValueError, TypeError):
        return default
    if min_val is not None and result < min_val:
        return default
    return result


def _safe_float(val: Any, default: float) -> float:
    """Coerce *val* to ``float``, returning *default* on failure or ``None``."""
    try:
        return float(default if val is None else val)
    except (ValueError, TypeError):
        return default


try:
    from fastapi import WebSocket  # noqa: TC002
    from pydantic import BaseModel, Field
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for hijack hub/routes: pip install 'provide-terminal[websocket]'") from _e


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


VALID_ROLES = frozenset({"viewer", "operator", "admin"})


@dataclass
class WorkerTermState:
    """Per-worker connection state held by :class:`~provide.terminal.hijack.hub.TermHub`."""

    worker_ws: WebSocket | None = None
    browsers: dict[WebSocket, str] = field(default_factory=dict)  # ws → role
    hijack_owner: WebSocket | None = None  # dashboard WS that holds the lease
    hijack_owner_expires_at: float | None = None
    hijack_session: HijackSession | None = None  # REST lease
    input_mode: str = "hijack"  # "hijack" | "open"
    last_snapshot: dict[str, Any] | None = None
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=2000))
    event_seq: int = 0
    min_event_seq: int = 0


# ---------------------------------------------------------------------------
# API request models (pydantic — used by FastAPI for request validation)
# ---------------------------------------------------------------------------


class HijackAcquireRequest(BaseModel):
    owner: str = Field("operator", min_length=1, max_length=200)
    lease_s: int = Field(90, ge=1, le=3600)


class HijackHeartbeatRequest(BaseModel):
    lease_s: int = Field(90, ge=1, le=3600)


class InputModeRequest(BaseModel):
    input_mode: str = Field(..., pattern=r"^(hijack|open)$")


class HijackSendRequest(BaseModel):
    keys: str = Field(..., max_length=10_000)
    expect_prompt_id: str | None = Field(None, max_length=200)
    expect_regex: str | None = Field(None, max_length=MAX_EXPECT_REGEX_LEN)
    timeout_ms: int = Field(2000, ge=100, le=30_000)
    poll_interval_ms: int = Field(120, ge=50, le=5_000)
