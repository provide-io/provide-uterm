#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Refuse a WebSocket before the upgrade with a chosen HTTP status.

Starlette's default WebSocket refusal is ``close()`` before ``accept()``, which
every ASGI server maps to a hardcoded **403**. That is the wrong answer for a
caller who simply did not authenticate: 401 says "say who you are", 403 says
"you may not", and the Go and C# ports both make that distinction. Emitting 401
needs the ASGI websocket-denial-response extension, which Starlette exposes as
``WebSocket.send_denial_response``.

The extension works — the client receives the status and the headers — but
uvicorn's sansio implementation then treats the finished denial as an unfinished
handshake and logs an ERROR for it (see ``_UVICORN_INCOMPLETE_HANDSHAKE``).
``install_ws_denial_support`` installs a filter that drops exactly that record,
and only for connections this module actually denied.
"""

from __future__ import annotations

import contextvars
import logging
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse
from starlette.status import HTTP_401_UNAUTHORIZED, WS_1008_POLICY_VIOLATION

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.websockets import WebSocket

__all__ = ["WebSocketAuthDenied", "handle_ws_auth_denied", "install_ws_denial_support"]

# The ASGI extension a server must advertise for a denial response to be
# deliverable. uvicorn advertises it on every websocket implementation, and so
# does Starlette's TestClient; a server that does not gets the 403 close below.
_DENIAL_EXTENSION = "websocket.http.response"

# The refusal body, matching this server's HTTP refusals and Go's detailError.
_DETAIL_KEY = "detail"

# RFC 7235: a 401 has to say how to authenticate. Module level, not inline:
# mutmut only mutates inside functions, and Starlette lowercases header names
# on the way out (responses.py init_headers), so an inline literal would spawn
# two case-mutants that are byte-identical to this one and cannot be killed.
_BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}

# The logger uvicorn reports a websocket protocol error on.
_UVICORN_LOGGER = "uvicorn.error"

# uvicorn's sansio websocket implementation logs this after a denial response
# that was delivered correctly. Its completion check tests only
# ``handshake_complete`` (websockets_sansio_impl.py:416) while the denial path
# sets ``initial_response`` instead; two adjacent branches in the same file test
# both, as does the wsproto implementation. The follow-on send_500_response() is
# a no-op — it returns early on ``initial_response`` — so the client is
# unaffected and this is purely a spurious log line. It is still one line per
# refusal, and refusals are the normal case on an authenticated endpoint, so a
# connection flood would otherwise amplify into an equal flood of ERROR records.
_UVICORN_INCOMPLETE_HANDSHAKE = "ASGI callable returned without completing handshake."

# Set on the connection's context once a denial has actually been written.
# uvicorn awaits the ASGI app and logs the message above from the same task, so
# a value set inside the app is still visible to the filter (verified: a denied
# connection observes True, a genuinely unfinished handshake observes False).
# That is what keeps a real "returned without completing handshake" bug audible.
_denial_sent: contextvars.ContextVar[bool] = contextvars.ContextVar("uterm_ws_denial_sent", default=False)


class WebSocketAuthDenied(Exception):  # noqa: N818 — a refusal, not an error condition
    """Refuse a WebSocket handshake with an explicit HTTP status."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _IncompleteHandshakeFilter(logging.Filter):
    """Drop uvicorn's spurious incomplete-handshake ERROR for our own denials."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.getMessage() != _UVICORN_INCOMPLETE_HANDSHAKE:
            return True
        return not _denial_sent.get()


async def handle_ws_auth_denied(websocket: WebSocket, exc: WebSocketAuthDenied) -> None:
    """Answer a refused handshake with ``exc.status_code`` instead of a 403 close."""
    if _DENIAL_EXTENSION not in websocket.scope.get("extensions", {}):
        # An ASGI server without the extension cannot carry a status, so fall
        # back to what Starlette would have done unaided: close before accept,
        # which the server reports to the client as 403.
        await websocket.close(code=WS_1008_POLICY_VIOLATION, reason=exc.detail)
        return

    headers = _BEARER_CHALLENGE if exc.status_code == HTTP_401_UNAUTHORIZED else None
    response = JSONResponse({_DETAIL_KEY: exc.detail}, status_code=exc.status_code, headers=headers)
    await websocket.send_denial_response(response)
    _denial_sent.set(True)


def install_ws_denial_support(app: FastAPI) -> None:
    """Wire the denial handler and silence uvicorn's spurious follow-on ERROR."""
    # Silenced with an ignore comment rather than typing.cast: cast discards its
    # first argument at runtime, so mutating that string is a no-op no test can
    # ever detect — an unkillable mutant for no benefit.
    app.add_exception_handler(WebSocketAuthDenied, handle_ws_auth_denied)  # type: ignore[arg-type]
    # The uvicorn logger is process-global while an app is not: a test suite
    # builds hundreds of apps, and adding a filter per app would stack hundreds
    # of them on one logger.
    uvicorn_logger = logging.getLogger(_UVICORN_LOGGER)
    if not any(isinstance(existing, _IncompleteHandshakeFilter) for existing in uvicorn_logger.filters):
        uvicorn_logger.addFilter(_IncompleteHandshakeFilter())
