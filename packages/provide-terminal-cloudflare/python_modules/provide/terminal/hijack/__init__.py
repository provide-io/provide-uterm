#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Hijack infrastructure for human-in-the-loop terminal takeover.

The hijack system lets a human operator pause an automated worker, send keystrokes
directly, step through individual loop iterations, and then resume automation.

Three layers:

- :class:`~provide.terminal.hijack.base.HijackableMixin` — mixin for the worker side.
  Drop into any async class; call :meth:`await_if_hijacked` at checkpoints.

- :class:`~provide.terminal.hijack.hub.TermHub` — server-side registry.
  Tracks which workers are connected, manages leases, routes input/output.

- :class:`~provide.terminal.hijack.bridge.TermBridge` — worker-side WS client.
  Connects the worker to the hub, forwards terminal output, receives control commands.

- :mod:`~provide.terminal.hijack.routes` — FastAPI WebSocket + REST routes.
  Mount via ``hub.create_router()`` onto any FastAPI app.

Requires the ``websocket`` extra for ``hub``, ``bridge``, and ``routes``::

    pip install 'provide-terminal[websocket]'

``base.py`` has no optional deps.
"""

from __future__ import annotations

from provide.terminal.hijack.base import HijackableMixin

__all__ = ["HijackableMixin"]
