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

import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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
