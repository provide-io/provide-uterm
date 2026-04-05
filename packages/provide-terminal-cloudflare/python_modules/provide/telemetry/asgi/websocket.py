# SPDX-FileCopyrightText: Copyright (C) 2026 provide.io llc
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""WebSocket telemetry helpers."""

from __future__ import annotations

from typing import Any

from provide.telemetry.headers import get_header
from provide.telemetry.logger.context import bind_context, reset_context, save_context

ContextToken = object


def bind_websocket_context(scope: dict[str, Any]) -> ContextToken:
    """Bind WebSocket headers into logger context. Returns a token for cleanup via clear_websocket_context()."""
    token = save_context()
    request_id = _extract_header(scope, b"x-request-id")
    session_id = _extract_header(scope, b"x-session-id")
    actor_id = _extract_header(scope, b"x-actor-id")
    if request_id is not None:
        bind_context(request_id=request_id)
    if session_id is not None:
        bind_context(session_id=session_id)
    if actor_id is not None:
        bind_context(actor_id=actor_id)
    return token


def clear_websocket_context(token: ContextToken) -> None:
    """Restore context to the state before bind_websocket_context() was called."""
    reset_context(token)  # type: ignore[arg-type]


def _extract_header(scope: dict[str, Any], key: bytes) -> str | None:
    return get_header(scope, key)
