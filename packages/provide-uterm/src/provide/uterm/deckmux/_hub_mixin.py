#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""DeckMux TermHub facade — thin shim over :class:`DeckMuxPresence`.

Phase 7 of refactor #16 finishes the TermHub mixin teardown by extracting
the DeckMux responsibility into :class:`DeckMuxPresence` (in
``_service.py``). This module remains as a thin facade because:

* The production server's :class:`TermHub` does not mix this in — only
  custom hubs (and tests) subclass ``DeckMuxMixin``.
* Server route code (``bridge/routes/websockets.py``) probes hubs via
  ``hasattr(hub, "deckmux_on_browser_connect")`` and the
  ``test_websockets_coverage`` suite monkey-patches the same names on
  hub instances; both paths require the methods to live on the host
  class.
* Several deckmux tests read ``hub._presence_stores`` /
  ``hub._transfer_managers`` and call ``hub._get_presence_store(...)``
  directly. The facade preserves those attributes by aliasing them to
  the service's containers (same dict objects, not copies).

The service owns the state and logic; the mixin only forwards. Wire
format, lock semantics, and the public ``hub.deckmux_*`` API are
identical to the pre-extraction implementation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

try:
    from provide.uterm.deckmux._service import DeckMuxPresence

    _HAS_DECKMUX = True
except ImportError:  # pragma: no cover
    _HAS_DECKMUX = False

if TYPE_CHECKING:
    from provide.uterm.deckmux._presence import PresenceStore
    from provide.uterm.deckmux._transfer import TransferManager


class DeckMuxMixin:
    """Mixin for TermHub to handle DeckMux presence messages.

    Owns nothing of its own — all state and logic live in the composed
    :class:`DeckMuxPresence` service stored on ``self.deckmux``. This
    facade exists so that:

    * Existing call sites can keep using ``hub.deckmux_on_browser_*``
      and the legacy ``hub._presence_stores`` / ``hub._get_presence_store``
      patterns.
    * Tests that monkey-patch ``hub.deckmux_on_browser_connect`` on a
      hub instance continue to work (instance attributes shadow these
      method bindings).

    Expects the host class to provide:
    - broadcast(worker_id, msg) — send to all browsers
    - _workers dict with WorkerTermState entries
    - _lock for thread safety
    """

    deckmux: DeckMuxPresence

    def _deckmux_init(self) -> None:
        """Initialise DeckMux state. Call from host ``__init__``.

        Constructs the :class:`DeckMuxPresence` service with a back
        reference to the host hub. ``hub.broadcast`` is resolved lazily
        at call time, so the host may assign ``broadcast`` *after*
        invoking ``_deckmux_init()``.
        """
        self.deckmux = DeckMuxPresence(self)

    # -- Backward-compatible container aliases ----------------------------
    # ``hub._presence_stores`` / ``hub._transfer_managers`` are read
    # directly by tests (and an e2e ssh test). Expose them as properties
    # backed by the service's containers — same dict objects, so
    # mutations via either name stay in sync.

    @property
    def _presence_stores(self) -> dict[str, PresenceStore]:
        return self.deckmux.presence_stores

    @property
    def _transfer_managers(self) -> dict[str, TransferManager]:
        return self.deckmux.transfer_managers

    def _get_presence_store(self, worker_id: str) -> PresenceStore:
        return self.deckmux.get_presence_store(worker_id)

    def _get_transfer_manager(
        self,
        worker_id: str,
        config: dict[str, Any] | None = None,
    ) -> TransferManager:
        return self.deckmux.get_transfer_manager(worker_id, config)

    async def deckmux_on_browser_connect(
        self,
        worker_id: str,
        ws: Any,
        role: str,
        principal: Any = None,
    ) -> dict[str, Any] | None:
        """Called when a browser connects. Returns presence_sync message to send."""
        return await self.deckmux.on_browser_connect(worker_id, ws, role, principal=principal)

    async def deckmux_on_browser_disconnect(
        self,
        worker_id: str,
        ws: Any,
        principal: Any = None,
    ) -> None:
        """Called when a browser disconnects. Broadcasts presence_leave."""
        await self.deckmux.on_browser_disconnect(worker_id, ws, principal=principal)

    async def deckmux_handle_message(
        self,
        worker_id: str,
        ws: Any,
        msg: dict[str, Any],
        principal: Any = None,
    ) -> None:
        """Route a DeckMux message from a browser."""
        await self.deckmux.handle_message(worker_id, ws, msg, principal=principal)

    def deckmux_cleanup(self, worker_id: str) -> None:
        """Clean up DeckMux state for a session."""
        self.deckmux.cleanup(worker_id)
