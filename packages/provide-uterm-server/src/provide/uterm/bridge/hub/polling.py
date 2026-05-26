#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Thin compatibility facade over :class:`PollingCoordinator`.

Phase 7 of refactor #16 extracted the snapshot-polling helpers into
:class:`provide.uterm.bridge.hub.polling_service.PollingCoordinator`.
The mixin remains because the existing TermHub composes its sibling
mixins via multiple inheritance and the following methods are still
addressed as ``hub.<name>`` from REST helpers, the AI/MCP integration
and the test suite:

* ``snapshot_matches`` — pure predicate.
* ``wait_for_snapshot`` — poll for a worker snapshot.
* ``wait_for_guard`` — poll until a snapshot satisfies prompt
  guards.

All forwarded methods are one-line pass-throughs to ``self.polling``
(the composed :class:`PollingCoordinator`). ``snapshot_matches`` is
exposed via ``staticmethod(...)`` so the original
``TermHub.snapshot_matches(...)`` call form keeps working without
allocating bound methods per hub instance.
"""

from __future__ import annotations

from typing import Any

from provide.uterm.bridge.hub.polling_service import PollingCoordinator


class _PollingMixin:
    """Compatibility facade forwarding to :attr:`TermHub.polling`.

    The coordinator (:class:`PollingCoordinator`) owns the actual
    implementation. This mixin exists only so the legacy
    ``hub.<method>`` call sites — including REST helpers and the AI/MCP
    integration that drives ``wait_for_guard`` — keep working unchanged.
    """

    # Typed handle to the composed service; set by TermHub.__init__.
    polling: PollingCoordinator

    # -- Static helper re-exposed via the coordinator --------------------

    snapshot_matches = staticmethod(PollingCoordinator.snapshot_matches)

    # -- Snapshot polling forwarders ------------------------------------

    async def wait_for_snapshot(self, worker_id: str, timeout_ms: int = 1500) -> dict[str, Any] | None:
        """Poll for a fresh snapshot from *worker_id*, waiting up to *timeout_ms* ms."""
        return await self.polling.wait_for_snapshot(worker_id, timeout_ms)

    async def wait_for_guard(
        self,
        worker_id: str,
        *,
        expect_prompt_id: str | None,
        expect_regex: str | None,
        timeout_ms: int,
        poll_interval_ms: int,
    ) -> tuple[bool, dict[str, Any] | None, str | None]:
        """Poll until the snapshot satisfies prompt-id/regex guards or *timeout_ms* elapses."""
        return await self.polling.wait_for_guard(
            worker_id,
            expect_prompt_id=expect_prompt_id,
            expect_regex=expect_regex,
            timeout_ms=timeout_ms,
            poll_interval_ms=poll_interval_ms,
        )


__all__ = ["_PollingMixin"]
