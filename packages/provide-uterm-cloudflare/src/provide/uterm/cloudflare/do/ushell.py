#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""CF Durable Object adapter for ushell.

Architecture
------------

::

    browser WS (xterm.js keystrokes)
          ↓
    DO.webSocketMessage() → push_worker_input(data)
          ↓
    _ushell.handle_input(data)   ← UshellConnector (provide.uterm.shell.terminal)
          ↓
    list[term frames]
          ↓
    broadcast_to_browsers(frame) for each frame

The DO detects ushell sessions by the ``ushell-`` prefix on ``worker_id``
(set in ``entry.py`` when ``connector_type == "ushell"`` is POSTed to
``/api/connect``).  No external worker process is required.

Public API
----------
``init_ushell(runtime)``
    Called from ``SessionRuntime.fetch()`` when a browser socket connects.
    Creates the ``UshellConnector`` and sets ``input_mode = "open"``.

``on_browser_connected(runtime)``
    Called from ``SessionRuntime.webSocketOpen()`` after a browser joins.
    On the first browser, broadcasts ``worker_connected`` and the welcome
    banner.  Subsequent browsers receive the standard hello/snapshot flow
    already handled by the DO.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Sentinel used when UshellConnector cannot be imported (missing dependency).
_IMPORT_ERROR: str | None = None


def _load_connector(session_id: str, env: Any, storage: Any = None) -> Any:
    """Import and instantiate UshellConnector, wiring CF env bindings."""
    global _IMPORT_ERROR
    try:
        from provide.uterm.shell.commands import CommandDispatcher  # noqa: F401
        from provide.uterm.shell.terminal._connector import UshellConnector
    except ImportError as exc:
        _IMPORT_ERROR = str(exc)
        logger.warning("ushell: could not import UshellConnector: %s", exc)
        return None

    # Build context dict with CF bindings available to commands.
    ctx: dict[str, Any] = {"env": env}
    if storage is not None:
        ctx["storage"] = storage
    try:
        from provide.uterm.cloudflare.state.registry import list_kv_sessions

        async def _list_sessions() -> list[dict[str, Any]]:
            return await list_kv_sessions(env)

        ctx["list_kv_sessions"] = _list_sessions
    except Exception:
        try:
            from state.registry import list_kv_sessions as _lks  # type: ignore[import-not-found]

            async def _list_sessions2() -> list[dict[str, Any]]:
                return await _lks(env)

            ctx["list_kv_sessions"] = _list_sessions2
        except Exception:
            pass

    return UshellConnector(session_id=session_id, extra_ctx=ctx)


def init_ushell(runtime: Any) -> None:
    """Create and attach a ``UshellConnector`` to *runtime* if applicable.

    Safe to call on every browser connect — no-op if already initialized or
    if the session is not an ushell session.

    Mutates *runtime*:
    - ``runtime._ushell``: the ``UshellConnector`` instance
    - ``runtime.input_mode``: set to ``"open"`` (all operators can type)
    """
    if not runtime.worker_id.startswith("ushell-"):
        return
    if runtime._ushell is not None:
        return
    storage = getattr(getattr(runtime, "ctx", None), "storage", None)
    connector = _load_connector(runtime.worker_id, runtime.env, storage=storage)
    if connector is None:
        logger.error("ushell: failed to load connector for %s", runtime.worker_id)
        return
    runtime.input_mode = "open"
    runtime._ushell = connector
    logger.debug("ushell: initialized for %s", runtime.worker_id)


async def on_browser_connected(runtime: Any) -> None:
    """Send welcome frames when the first browser connects to an ushell session.

    Subsequent browser connections are handled by the standard DO hello/
    snapshot flow; this function is a no-op once ``runtime._ushell_started``
    is True.
    """
    if runtime._ushell is None:
        return
    if runtime._ushell_started:
        return

    await runtime._ushell.start()
    runtime._ushell_started = True

    # Broadcast worker_connected so all browsers see "Live" in the widget.
    await runtime.broadcast_worker_frame(
        {"type": "worker_connected", "worker_id": runtime.worker_id, "ts": time.time()}
    )

    # Send welcome banner + initial prompt.
    for frame in await runtime._ushell.poll_messages():
        if frame.get("type") == "worker_hello":
            # Already handled: input_mode set in init_ushell(); skip the frame.
            continue
        await runtime.broadcast_to_browsers(frame)
