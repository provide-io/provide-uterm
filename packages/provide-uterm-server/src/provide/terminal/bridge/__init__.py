#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Bridge — session control infrastructure for human-in-the-loop terminal takeover.

The bridge system lets a human operator pause an automated worker, send keystrokes
directly, step through individual loop iterations, and then resume automation.

Three layers:

- :class:`~provide.terminal.bridge.base.HijackableMixin` — mixin for the worker side.
  Drop into any async class; call :meth:`await_if_hijacked` at checkpoints.

- :class:`~provide.terminal.bridge.hub.TermHub` — server-side registry.
  Tracks which workers are connected, manages leases, routes input/output.

- :class:`~provide.terminal.bridge.worker_link.TermBridge` — worker-side WS client.
  Connects the worker to the hub, forwards terminal output, receives control commands.

- :mod:`~provide.terminal.bridge.routes` — FastAPI WebSocket + REST routes.
  Mount via ``hub.create_router()`` onto any FastAPI app.

Requires the ``websocket`` extra for ``hub``, ``worker_link``, and ``routes``::

    pip install 'provide-uterm[websocket]'

``base.py`` has no optional deps.
"""

from __future__ import annotations

import pkgutil

__path__ = pkgutil.extend_path(__path__, __name__)

from provide.terminal.bridge.base import HijackableMixin

__all__ = ["HijackableMixin"]
