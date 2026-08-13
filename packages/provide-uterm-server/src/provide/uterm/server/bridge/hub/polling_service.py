#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""PollingCoordinator: snapshot polling extracted from ``_PollingMixin``.

Phase 7 of refactor #16 lifts the snapshot-polling helpers from the
mixin pile into a service class composed on :class:`TermHub` as
``self.polling``. Same shape as the other Phase 4-7 services: a single
back reference to the hub, no behavioural or lock-semantics changes.

Scope:

* ``snapshot_matches`` — pure predicate against a snapshot dict.
* ``wait_for_snapshot`` — request a fresh snapshot from the worker and
  poll until either it arrives (newer than the request timestamp) or
  the timeout elapses.
* ``wait_for_guard`` — poll until a snapshot satisfies prompt-id/regex
  guards or the timeout elapses, re-requesting snapshots from the
  worker when the timestamp has not advanced.

The static helper ``snapshot_matches`` stays as ``@staticmethod`` so
the mixin shim can re-expose it via ``staticmethod(...)`` without
allocating a bound method per hub instance.

Worker-bound snapshot requests are dispatched through ``hub.request_snapshot``
(rather than directly through ``hub.presence_mgr.request_snapshot``) so
the test-only pattern of monkey-patching ``hub.request_snapshot`` keeps
working — :mod:`provide.uterm.server.bridge.hub.connections` documents that
contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger
from provide.uterm.server.bridge.rest_helpers import (
    PromptRegexError,
    compile_expect_regex,
)
from provide.uterm.server.bridge.rest_helpers import (
    snapshot_matches as shared_snapshot_matches,
)

if TYPE_CHECKING:
    from provide.uterm.server.bridge.hub.core import TermHub
    from provide.uterm.server.bridge.models import WorkerTermState


logger = get_logger(__name__)


class PollingCoordinator:
    """Snapshot polling helper service composed into :class:`TermHub`.

    Args:
        hub: The composing :class:`TermHub`. The coordinator uses
            ``hub._lock``, ``hub.registry`` and ``hub.request_snapshot``
            (kept on the hub so test monkey-patching keeps working).
    """

    __slots__ = ("_hub",)

    def __init__(self, hub: TermHub) -> None:
        self._hub = hub

    # -- Pure predicate -------------------------------------------------

    @staticmethod
    def snapshot_matches(
        snapshot: dict[str, Any] | None,
        *,
        expect_prompt_id: str | None,
        expect_regex: re.Pattern[str] | None,
    ) -> bool:
        """Return True if *snapshot* satisfies the prompt-id and/or regex guard."""
        return shared_snapshot_matches(snapshot, expect_prompt_id=expect_prompt_id, expect_regex=expect_regex)

    # -- Snapshot polling -----------------------------------------------

    async def wait_for_snapshot(self, worker_id: str, timeout_ms: int = 1500) -> dict[str, Any] | None:
        """Poll for a fresh snapshot from *worker_id*, waiting up to *timeout_ms* ms."""
        hub = self._hub
        req_ts = time.time()  # wall-clock — compared against worker snapshot ts
        end = time.monotonic() + timeout_ms / 1000.0
        await hub.request_snapshot(worker_id)
        while time.monotonic() < end:
            async with hub._lock:
                st: WorkerTermState | None = hub.registry.get(worker_id)
                if st is None:
                    return None
                snap = st.last_snapshot
            if snap is not None and snap.get("ts", 0) > req_ts:
                return snap
            await asyncio.sleep(0.08)
        # Timed out: no snapshot NEWER than this request arrived. The caller
        # gets None and typically falls back to the cached last_snapshot, so a
        # client sees a stale screen that is indistinguishable from an idle
        # one. Record the staleness explicitly — a poll that silently returns
        # old state is exactly how a wedged worker looks like a quiet game.
        #
        # Narrowed since TermBridge began PUSHING snapshots on screen change:
        # the loop above accepts any snapshot fresher than req_ts, so a push
        # that happens to land inside the window releases the wait and this
        # warning never fires — even if request_snapshot reached nobody. So a
        # silent period here means "no fresh screen state arrived by any route",
        # NOT "the request path works". A request path broken on a busy screen
        # is now invisible to this diagnostic; only a quiet screen exposes it.
        # test_unsolicited_snapshot_integration.py pins that behaviour.
        stale_age = None
        if snap is not None:
            with contextlib.suppress(TypeError, ValueError):
                stale_age = round(req_ts - float(snap.get("ts", 0)), 3)
        logger.warning(
            "snapshot_wait_timeout",
            worker_id=worker_id,
            timeout_ms=timeout_ms,
            had_cached=snap is not None,
            cached_age_s=stale_age,
        )
        return None

    @staticmethod
    def _compile_guard_regex(
        expect_regex: str | None,
    ) -> tuple[re.Pattern[str] | None, str | None]:
        """Compile *expect_regex* and return ``(pattern, error_msg)``.

        Returns ``(None, None)`` when *expect_regex* is absent/empty.
        Returns ``(None, error_message)`` if compilation fails.
        Returns ``(compiled_pattern, None)`` on success.
        """
        if not expect_regex:
            return None, None
        try:
            return compile_expect_regex(expect_regex, flags=re.IGNORECASE | re.MULTILINE), None
        except PromptRegexError as exc:
            return None, str(exc)

    async def wait_for_guard(
        self,
        worker_id: str,
        *,
        expect_prompt_id: str | None,
        expect_regex: str | None,
        timeout_ms: int,
        poll_interval_ms: int,
    ) -> tuple[bool, dict[str, Any] | None, str | None]:
        """Poll until the snapshot satisfies prompt-id/regex guards or *timeout_ms* elapses.

        Returns ``(matched, snapshot, reason)`` where *reason* is None on success
        or a short error string on failure.
        """
        hub = self._hub
        regex_obj, regex_err = self._compile_guard_regex(expect_regex)
        if regex_err is not None:
            return False, None, regex_err

        if not expect_prompt_id and regex_obj is None:
            async with hub._lock:
                st = hub.registry.get(worker_id)
                snap = st.last_snapshot if st is not None else None
            await hub.request_snapshot(worker_id)
            return True, snap, None

        end = time.monotonic() + max(50, timeout_ms) / 1000.0
        interval = max(20, poll_interval_ms) / 1000.0
        last_snapshot: dict[str, Any] | None = None
        # Request an initial snapshot before entering the loop; subsequent
        # requests are only sent when the snapshot timestamp has not advanced
        # since the previous poll, avoiding flooding the worker channel when
        # the worker is already streaming snapshots proactively.
        await hub.request_snapshot(worker_id)
        last_snap_ts = 0.0
        while time.monotonic() < end:
            async with hub._lock:
                st = hub.registry.get(worker_id)
                last_snapshot = st.last_snapshot if st is not None else None
            if self.snapshot_matches(
                last_snapshot,
                expect_prompt_id=expect_prompt_id,
                expect_regex=regex_obj,
            ):
                return True, last_snapshot, None
            snap_ts = last_snapshot.get("ts", 0.0) if last_snapshot else 0.0
            if snap_ts <= last_snap_ts:
                # No new snapshot since the last poll — nudge the worker again.
                await hub.request_snapshot(worker_id)
            last_snap_ts = snap_ts
            await asyncio.sleep(interval)

        return False, last_snapshot, "prompt_guard_not_satisfied"


__all__ = ["PollingCoordinator"]
