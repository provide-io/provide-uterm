#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Server bridge runtime for human-in-the-loop terminal takeover.

The bridge system lets a human operator pause an automated worker, send keystrokes
directly, step through individual loop iterations, and then resume automation.

Three layers:

- :class:`~provide.uterm.bridge.base.HijackableMixin` — mixin for the worker side.
  Drop into any async class; call :meth:`await_if_hijacked` at checkpoints.

- :class:`~provide.uterm.server.bridge.hub.TermHub` — server-side registry.
  Tracks which workers are connected, manages leases, routes input/output.

- :class:`~provide.uterm.server.bridge.worker_link.TermBridge` — worker-side WS client.
  Connects the worker to the hub, forwards terminal output, receives control commands.

- :mod:`~provide.uterm.server.bridge.routes` — FastAPI WebSocket + REST routes.
  Mount via ``hub.create_router()`` onto any FastAPI app.

Requires the ``websocket`` extra for ``hub``, ``worker_link``, and ``routes``::

    pip install 'provide-uterm[websocket]'

The user-facing, dependency-light bridge primitives remain in
``provide.uterm.bridge``.
"""

from __future__ import annotations

__all__: list[str] = []
