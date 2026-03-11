#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Snapshot polling mixin for TermHub.

Extracted from ``core.py`` to keep file sizes under 500 LOC.
Provides ``snapshot_matches``, ``wait_for_snapshot``, and ``wait_for_guard``.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any

from provide.terminal.hijack.models import extract_prompt_id

if TYPE_CHECKING:
    from provide.terminal.hijack.models import WorkerTermState


class _PollingMixin:
    """Mixin providing snapshot polling methods for TermHub."""

    @staticmethod
    def snapshot_matches(
        snapshot: dict[str, Any] | None,
        *,
        expect_prompt_id: str | None,
        expect_regex: re.Pattern[str] | None,
    ) -> bool:
        """Return True if *snapshot* satisfies the prompt-id and/or regex guard."""
        if snapshot is None:
            return False
        if expect_prompt_id and extract_prompt_id(snapshot) != expect_prompt_id:
            return False
        return not (expect_regex is not None and not expect_regex.search(str(snapshot.get("screen", ""))))

    async def wait_for_snapshot(self, worker_id: str, timeout_ms: int = 1500) -> dict[str, Any] | None:
        """Poll for a fresh snapshot from *worker_id*, waiting up to *timeout_ms* ms."""
        req_ts = time.time()
        end = req_ts + timeout_ms / 1000.0
        await self.request_snapshot(worker_id)  # type: ignore[attr-defined]
        while time.time() < end:
            async with self._lock:  # type: ignore[attr-defined]
                st: WorkerTermState | None = self._workers.get(worker_id)  # type: ignore[attr-defined]
                if st is None:
                    return None
                snap = st.last_snapshot
            if snap is not None and snap.get("ts", 0) > req_ts:
                return snap
            await asyncio.sleep(0.08)
        return None

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
        regex_obj: re.Pattern[str] | None = None
        if expect_regex:
            try:
                regex_obj = re.compile(expect_regex, re.IGNORECASE | re.MULTILINE)
            except re.error as exc:
                return False, None, f"invalid expect_regex: {exc}"

        if not expect_prompt_id and regex_obj is None:
            async with self._lock:  # type: ignore[attr-defined]
                st = self._workers.get(worker_id)  # type: ignore[attr-defined]
                snap = st.last_snapshot if st is not None else None
            await self.request_snapshot(worker_id)  # type: ignore[attr-defined]
            return True, snap, None

        end = time.time() + max(50, timeout_ms) / 1000.0
        interval = max(20, poll_interval_ms) / 1000.0
        last_snapshot: dict[str, Any] | None = None
        while time.time() < end:
            async with self._lock:  # type: ignore[attr-defined]
                st = self._workers.get(worker_id)  # type: ignore[attr-defined]
                last_snapshot = st.last_snapshot if st is not None else None
            if self.snapshot_matches(
                last_snapshot,
                expect_prompt_id=expect_prompt_id,
                expect_regex=regex_obj,
            ):
                return True, last_snapshot, None
            await self.request_snapshot(worker_id)  # type: ignore[attr-defined]
            await asyncio.sleep(interval)

        return False, last_snapshot, "prompt_guard_not_satisfied"
