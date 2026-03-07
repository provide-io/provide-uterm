#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FastAPI WebSocket + REST routes for the terminal hijack hub.

Mount via :meth:`~provide.terminal.hijack.hub.TermHub.create_router`::

    hub = TermHub()
    app.include_router(hub.create_router())

WebSocket endpoints:
- ``/ws/worker/{worker_id}/term``  — worker → hub (terminal output, snapshots)
- ``/ws/browser/{worker_id}/term`` — browser → hub (dashboard viewer + hijack control)

REST endpoints (require a live hijack session):
- ``POST /worker/{id}/hijack/acquire``
- ``POST /worker/{id}/hijack/{hid}/heartbeat``
- ``GET  /worker/{id}/hijack/{hid}/snapshot``
- ``GET  /worker/{id}/hijack/{hid}/events``
- ``POST /worker/{id}/hijack/{hid}/send``
- ``POST /worker/{id}/hijack/{hid}/step``
- ``POST /worker/{id}/hijack/{hid}/release``
"""

from __future__ import annotations

from provide.terminal.hijack.routes.rest import register_rest_routes as register_rest_routes
from provide.terminal.hijack.routes.websockets import register_ws_routes as register_ws_routes

__all__ = ["register_rest_routes", "register_ws_routes"]
