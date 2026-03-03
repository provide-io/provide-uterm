#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FastAPI WebSocket + REST routes for the terminal hijack hub.

Mount via :meth:`~provide.terminal.hijack.hub.TermHub.create_router`::

    hub = TermHub()
    app.include_router(hub.create_router())

WebSocket endpoints:
- ``/ws/worker/{bot_id}/term``  — worker → hub (terminal output, snapshots)
- ``/ws/bot/{bot_id}/term``     — browser → hub (dashboard viewer + hijack control)

REST endpoints (require a live hijack session):
- ``POST /bot/{id}/hijack/acquire``
- ``POST /bot/{id}/hijack/{hid}/heartbeat``
- ``GET  /bot/{id}/hijack/{hid}/snapshot``
- ``GET  /bot/{id}/hijack/{hid}/events``
- ``POST /bot/{id}/hijack/{hid}/send``
- ``POST /bot/{id}/hijack/{hid}/step``
- ``POST /bot/{id}/hijack/{hid}/release``
"""

from __future__ import annotations

from provide.terminal.hijack.routes_rest import register_rest_routes as register_rest_routes
from provide.terminal.hijack.routes_ws import register_ws_routes as register_ws_routes

__all__ = ["register_ws_routes", "register_rest_routes"]
