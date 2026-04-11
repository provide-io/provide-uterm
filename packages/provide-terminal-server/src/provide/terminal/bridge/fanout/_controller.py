#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""FanOutController — orchestrator for fan-out groups and broadcast input."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import TYPE_CHECKING

from provide.terminal.bridge.fanout._collector import OutputCollector
from provide.terminal.bridge.fanout._divergence import compute_divergence
from provide.terminal.bridge.fanout._models import FanOutResult, SessionFanOutResult
from provide.terminal.bridge.fanout._store import InMemoryFanOutStore

if TYPE_CHECKING:
    from provide.terminal.bridge.fanout._models import FanOutGroup
    from provide.terminal.bridge.fanout._store import FanOutStore
    from provide.terminal.bridge.hub import TermHub


class FanOutController:
    """Orchestrates fan-out groups and broadcasts input to multiple sessions."""

    def __init__(
        self,
        hub: TermHub,
        *,
        store: FanOutStore | None = None,
        max_group_size: int = 50,
    ) -> None:
        self._hub = hub
        self._store: FanOutStore = store if store is not None else InMemoryFanOutStore()
        self._max_group_size = max_group_size

    # -- Group CRUD --------------------------------------------------------

    async def create_group(self, group: FanOutGroup, *, principal: str) -> str:
        """Validate and persist a new fan-out group. Returns the group_id."""
        if len(group.worker_ids) > self._max_group_size:
            msg = f"Group size {len(group.worker_ids)} exceeds max {self._max_group_size}"
            raise ValueError(msg)
        group.created_by = principal
        await self._store.save(group)
        return group.group_id

    async def delete_group(self, group_id: str, *, principal: str) -> None:
        """Delete a fan-out group. Only the creator can delete."""
        group = await self._authorized_group(group_id, principal)
        if group is not None:
            await self._store.delete(group_id)

    async def get_group(self, group_id: str, *, principal: str) -> FanOutGroup | None:
        """Retrieve a group if *principal* is the creator or a grantee."""
        return await self._authorized_group(group_id, principal)

    async def list_groups(self, principal: str) -> list[FanOutGroup]:
        """List all groups visible to the given principal."""
        return await self._store.list_for_principal(principal)

    async def grant_access(self, group_id: str, grantee: str, *, principal: str) -> None:
        """Add *grantee* to the group's grants list. Only the creator can grant."""
        group = await self._store.get(group_id)
        if group is None or group.created_by != principal:
            return
        if grantee not in group.grants:
            group.grants.append(grantee)
            await self._store.save(group)

    async def _authorized_group(self, group_id: str, principal: str) -> FanOutGroup | None:
        """Return group if *principal* is the creator or a grantee, else None."""
        group = await self._store.get(group_id)
        if group is None:
            return None
        if group.created_by == principal or principal in group.grants:
            return group
        return None

    # -- Send --------------------------------------------------------------

    async def send(
        self,
        group_id: str,
        data: str,
        *,
        principal: str,
        quiesce_ms: int | None = None,
        max_response_ms: int | None = None,
    ) -> FanOutResult:
        """Broadcast *data* to all workers in the group and collect results."""
        group = await self._authorized_group(group_id, principal)
        if group is None:
            return FanOutResult(
                group_id=group_id,
                send_id=uuid.uuid4().hex,
                command=data,
                sent_at=time.time(),
                results=[],
                divergent_sessions=[],
                failed_sessions=[],
            )

        q_ms = quiesce_ms if quiesce_ms is not None else group.quiesce_ms
        m_ms = max_response_ms if max_response_ms is not None else group.max_response_ms

        if group.mode == "sequential":
            return await self._send_sequential(group, data, q_ms, m_ms)
        return await self._send_parallel(group, data, q_ms, m_ms)

    # -- Parallel ----------------------------------------------------------

    async def _send_parallel(
        self,
        group: FanOutGroup,
        data: str,
        quiesce_ms: int,
        max_response_ms: int,
    ) -> FanOutResult:
        send_id = uuid.uuid4().hex
        sent_at = time.time()
        frame = {"type": "input", "data": data, "ts": sent_at}

        # Send to all workers in parallel
        send_results = await asyncio.gather(
            *(self._hub.send_worker(wid, frame) for wid in group.worker_ids),
            return_exceptions=True,
        )

        # Collect output from workers that accepted the send
        async def _collect(wid: str) -> tuple[str, int]:
            collector = OutputCollector()
            return await collector.collect(self._hub, wid, quiesce_ms=quiesce_ms, max_ms=max_response_ms)

        tasks: list[asyncio.Task[tuple[str, int]]] = []
        for wid, ok in zip(group.worker_ids, send_results, strict=True):
            if ok is True:
                tasks.append(asyncio.create_task(_collect(wid)))

        collected: list[tuple[str, int] | BaseException] = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []

        # Build per-session results
        results: list[SessionFanOutResult] = []
        failed_sessions: list[str] = []
        successful_outputs: list[str] = []
        successful_indices: list[int] = []
        collect_idx = 0

        for wid, ok in zip(group.worker_ids, send_results, strict=True):
            if ok is True:
                item = collected[collect_idx]
                collect_idx += 1
                if isinstance(item, BaseException):
                    results.append(SessionFanOutResult(
                        worker_id=wid, ok=False, output_delta=None, elapsed_ms=0, divergent=False,
                    ))
                    failed_sessions.append(wid)
                else:
                    delta, elapsed = item
                    results.append(SessionFanOutResult(
                        worker_id=wid, ok=True, output_delta=delta, elapsed_ms=elapsed, divergent=False,
                    ))
                    successful_outputs.append(delta)
                    successful_indices.append(len(results) - 1)
            else:
                results.append(SessionFanOutResult(
                    worker_id=wid, ok=False, output_delta=None, elapsed_ms=0, divergent=False,
                ))
                failed_sessions.append(wid)

        # Compute divergence on successful outputs
        divergent_sessions: list[str] = []
        if successful_outputs:
            flags = compute_divergence(successful_outputs, threshold=group.divergence_threshold)
            for flag, idx in zip(flags, successful_indices, strict=True):
                if flag:
                    results[idx].divergent = True
                    divergent_sessions.append(results[idx].worker_id)

        return FanOutResult(
            group_id=group.group_id,
            send_id=send_id,
            command=data,
            sent_at=sent_at,
            results=results,
            divergent_sessions=divergent_sessions,
            failed_sessions=failed_sessions,
        )

    # -- Sequential --------------------------------------------------------

    async def _send_sequential(
        self,
        group: FanOutGroup,
        data: str,
        quiesce_ms: int,
        max_response_ms: int,
    ) -> FanOutResult:
        send_id = uuid.uuid4().hex
        sent_at = time.time()
        frame = {"type": "input", "data": data, "ts": sent_at}

        results: list[SessionFanOutResult] = []
        failed_sessions: list[str] = []
        successful_outputs: list[str] = []
        successful_indices: list[int] = []
        stopped = False

        for wid in group.worker_ids:
            if stopped:
                results.append(SessionFanOutResult(
                    worker_id=wid, ok=False, output_delta=None, elapsed_ms=0, divergent=False,
                ))
                failed_sessions.append(wid)
                continue

            ok = await self._hub.send_worker(wid, frame)
            if not ok:
                results.append(SessionFanOutResult(
                    worker_id=wid, ok=False, output_delta=None, elapsed_ms=0, divergent=False,
                ))
                failed_sessions.append(wid)
                continue

            collector = OutputCollector()
            delta, elapsed = await collector.collect(self._hub, wid, quiesce_ms=quiesce_ms, max_ms=max_response_ms)
            results.append(SessionFanOutResult(
                worker_id=wid, ok=True, output_delta=delta, elapsed_ms=elapsed, divergent=False,
            ))
            successful_outputs.append(delta)
            successful_indices.append(len(results) - 1)

            # Check error pattern
            if group.stop_on_first_error and group.error_pattern is not None and re.search(group.error_pattern, delta):
                stopped = True

        # Compute divergence on successful outputs
        divergent_sessions: list[str] = []
        if successful_outputs:
            flags = compute_divergence(successful_outputs, threshold=group.divergence_threshold)
            for flag, idx in zip(flags, successful_indices, strict=True):
                if flag:
                    results[idx].divergent = True
                    divergent_sessions.append(results[idx].worker_id)

        return FanOutResult(
            group_id=group.group_id,
            send_id=send_id,
            command=data,
            sent_at=sent_at,
            results=results,
            divergent_sessions=divergent_sessions,
            failed_sessions=failed_sessions,
        )
