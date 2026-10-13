#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Close the live sockets of a session's revoked share viewers.

A share viewer's token is judged when its socket opens and never again, so a
cutoff recorded by ``AuthorizationService.revoke_share_viewers`` stops the next
connection but not the ones already open. This closes those: every browser
socket on the session that holds the ``viewer`` role and whose principal the
cutoff now refuses. Owners, operators, admins and the hijack holder are never
share viewers, so they keep their sockets.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

from provide.uterm.server.authorization import is_share_viewer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from provide.uterm.server.authorization import AuthorizationService

# An application close code (4000-4999), so a client can tell "your access was
# taken away" from a network drop or a policy refusal (1008) and stop
# reconnecting. 4410 echoes HTTP 410 Gone: the grant existed and no longer does.
VIEWER_REVOKED_CLOSE_CODE = 4410
# Generic on purpose: the embedder knows why (a game made private, a share
# withdrawn) and can say so in its own words.
VIEWER_REVOKED_REASON = "viewer access revoked"


async def close_revoked_viewers(hub: Any, authz: AuthorizationService, session_id: str) -> int:
    """Close every browser socket on ``session_id`` held by a revoked share viewer.

    Returns how many sockets were closed. The sockets are chosen under the hub
    lock and closed outside it: a close awaits the network, and the socket's own
    receive loop needs the lock to run its disconnect bookkeeping.
    """
    async with hub._lock:
        state = hub.registry.get(session_id)
        browsers = [] if state is None else list(state.browsers.items())
    targets = [
        ws
        for ws, role in browsers
        if role == "viewer" and _revoked(authz, getattr(ws.state, "uterm_principal", None), session_id)
    ]
    for ws in targets:
        # A socket that is already closing has nothing left to tell.
        with contextlib.suppress(Exception):
            await ws.close(code=VIEWER_REVOKED_CLOSE_CODE, reason=VIEWER_REVOKED_REASON)
    return len(targets)


def _revoked(authz: AuthorizationService, principal: Any, session_id: str) -> bool:
    return principal is not None and authz.share_viewer_revoked(principal, session_id)


def socket_share_viewer_revoked(websocket: Any, session_id: str) -> bool:
    """True when the socket's own principal is a share viewer the cutoff refuses.

    Re-checked right after a browser registers. The revoke records its cutoff
    and then scans the registered sockets under the hub lock, so a socket whose
    role was resolved before the cutoff but which registered after the scan is
    caught here instead: either the scan saw it, or this sees the cutoff. A hub
    used outside the server app has no authorization service and revokes nothing.
    """
    # Through the ASGI scope rather than ``websocket.app``: that property is a
    # bare ``scope["app"]`` and raises KeyError for a hub mounted without one.
    app = getattr(websocket, "scope", {}).get("app")
    authz = getattr(getattr(app, "state", None), "uterm_authz", None)
    return authz is not None and _revoked(authz, getattr(websocket.state, "uterm_principal", None), session_id)


# The last event a share viewer's event stream gets before it is ended.
STREAM_REVOKED_EVENT = (
    "data: " + json.dumps({"type": "viewer_access_revoked", "reason": VIEWER_REVOKED_REASON}) + "\n\n"
)


class ShareViewerStreams:
    """The open event streams of share viewers, so a revoke can end them."""

    def __init__(self) -> None:
        self._open: dict[str, dict[asyncio.Event, Any]] = {}

    def open(self, session_id: str, principal: Any) -> asyncio.Event:
        ended = asyncio.Event()
        self._open.setdefault(session_id, {})[ended] = principal
        return ended

    def close(self, session_id: str, ended: asyncio.Event) -> None:
        streams = self._open.get(session_id, {})
        streams.pop(ended, None)
        if not streams:
            self._open.pop(session_id, None)

    def revoke(self, authz: AuthorizationService, session_id: str) -> int:
        """End every open stream on the session whose principal the cutoff refuses."""
        ended = [
            event
            for event, principal in self._open.get(session_id, {}).items()
            if authz.share_viewer_revoked(principal, session_id)
        ]
        for event in ended:
            event.set()
        return len(ended)


def share_viewer_streams(app: Any) -> ShareViewerStreams:
    """The app's stream registry, created on first use."""
    streams = getattr(app.state, "uterm_share_viewer_streams", None)
    if streams is None:
        streams = ShareViewerStreams()
        app.state.uterm_share_viewer_streams = streams
    return streams


async def stream_until_revoked(
    stream: AsyncIterator[str],
    streams: ShareViewerStreams,
    authz: AuthorizationService,
    principal: Any,
    session_id: str,
) -> AsyncIterator[str]:
    """Relay ``stream``, ending it with ``STREAM_REVOKED_EVENT`` if the viewer is revoked.

    Only a share viewer's stream is watched; anyone else's passes straight
    through. The check after registering closes the same race the socket
    check does: a revoke between the open-time read check and registration.
    """
    if not is_share_viewer(principal, session_id):
        async for item in stream:
            yield item
        return
    ended = streams.open(session_id, principal)
    try:
        if authz.share_viewer_revoked(principal, session_id):
            yield STREAM_REVOKED_EVENT
            return
        iterator = aiter(stream)
        stop = asyncio.ensure_future(ended.wait())
        try:
            while True:
                pending: asyncio.Future[str] = asyncio.ensure_future(anext(iterator))
                waiting: set[asyncio.Future[Any]] = {pending, stop}
                await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                if stop.done():
                    pending.cancel()
                    with contextlib.suppress(BaseException):
                        await pending
                    yield STREAM_REVOKED_EVENT
                    return
                try:
                    value = pending.result()
                except StopAsyncIteration:
                    return
                yield value
        finally:
            stop.cancel()
    finally:
        streams.close(session_id, ended)
