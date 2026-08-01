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
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from provide.uterm.server.bridge.fanout._collector import OutputCollector
from provide.uterm.server.bridge.fanout._divergence import compute_divergence
from provide.uterm.server.bridge.fanout._models import FanOutResult, SessionFanOutResult
from provide.uterm.server.bridge.fanout._store import InMemoryFanOutStore
from provide.uterm.server.bridge.hub.ext import FanOutPolicyGate, PolicyDecision
from provide.uterm.server.bridge.rest_helpers import compile_expect_regex

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from provide.uterm.server.bridge.fanout._models import FanOutGroup
    from provide.uterm.server.bridge.fanout._store import FanOutStore
    from provide.uterm.server.bridge.hub import TermHub
    from provide.uterm.server.bridge.identity import Principal
    from provide.uterm.server.models import SessionDefinition


class FanOutController:
    """Orchestrates fan-out groups and broadcasts input to multiple sessions."""

    def __init__(
        self,
        hub: TermHub,
        *,
        store: FanOutStore | None = None,
        max_group_size: int = 50,
        fanout_policy_gate: FanOutPolicyGate | None = None,
        is_global_admin: Callable[[Principal], Awaitable[bool]] | None = None,
        resolve_session: Callable[[str], Awaitable[SessionDefinition | None]] | None = None,
        can_read_session: Callable[[Principal, SessionDefinition], Awaitable[bool]] | None = None,
        allow_unknown_members: bool = False,
    ) -> None:
        self._hub = hub
        self._store: FanOutStore = store if store is not None else InMemoryFanOutStore()
        self._max_group_size = max_group_size
        self._fanout_policy_gate = fanout_policy_gate
        self._is_global_admin = is_global_admin
        self._resolve_session = resolve_session
        self._can_read_session = can_read_session
        self.allow_unknown_members = allow_unknown_members
        self._pending_approvals: dict[str, dict[str, Any]] = {}

        # Subscribe to approval expiration to prune pending state
        hub_approvals = getattr(self._hub, "approval_store", None)
        if hub_approvals:
            hub_approvals.on_expired = self._on_approval_expired

    def _on_approval_expired(self, request_id: str) -> None:
        """Prune local state when a fan-out approval times out in the Hub."""
        self._pending_approvals.pop(request_id, None)

    def _get_fanout_policy_gate(self) -> FanOutPolicyGate:
        if self._fanout_policy_gate is not None:
            return self._fanout_policy_gate

        class NoOpFanOutGate:
            async def intercept_fanout(self, command: str, context: Any, group_id: str) -> PolicyDecision:
                return PolicyDecision(action="allow")

        # ``FanOutPolicyGate`` is a Protocol; ``NoOpFanOutGate`` matches its
        # ``intercept_fanout`` signature structurally. The cast tells mypy
        # that's intentional — runtime checks the Protocol via duck typing.
        return cast("FanOutPolicyGate", NoOpFanOutGate())

    # -- Group CRUD --------------------------------------------------------

    async def create_group(self, group: FanOutGroup, *, principal: str | Principal) -> str:
        """Validate and persist a new fan-out group. Returns the group_id."""
        if len(group.worker_ids) > self._max_group_size:
            msg = f"Group size {len(group.worker_ids)} exceeds max {self._max_group_size}"
            raise ValueError(msg)
        # error_pattern is caller-supplied and re.search'd against every output
        # delta — an unbounded or pathological pattern is a ReDoS vector. Bound
        # its length and validate it compiles at creation time (PromptRegexError
        # is a ValueError, so the REST route maps it to a 400) rather than
        # letting it reach the per-delta match path.
        compile_expect_regex(group.error_pattern)
        group.created_by = self._principal_id(principal)
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
        await self._store.grant_access(group_id, grantee, principal)

    @staticmethod
    def _principal_id(principal: str | Principal) -> str:
        return principal if isinstance(principal, str) else principal.subject_id

    async def _authorized_group(self, group_id: str, principal: str | Principal) -> FanOutGroup | None:
        """Return group if *principal* is the creator or a grantee, else None."""
        group = await self._store.get(group_id)
        if group is None:
            return None
        principal_id = self._principal_id(principal)
        if group.created_by == principal_id or principal_id in group.grants:
            return group
        return None

    async def validate_members(self, worker_ids: list[str], principal: Principal) -> tuple[list[str], list[str]]:
        """Return currently authorized and refused members for a principal."""
        if self._resolve_session is None or self._can_read_session is None:
            return [], list(worker_ids)
        allowed: list[str] = []
        refused: list[str] = []
        for worker_id in worker_ids:
            definition = await self._resolve_session(worker_id)
            if definition is None or not await self._can_read_session(principal, definition):
                refused.append(worker_id)
            else:
                allowed.append(worker_id)
        return allowed, refused

    @staticmethod
    def _error_result(group_id: str, data: str, error: str) -> FanOutResult:
        """Build an explicit fail-closed result without dispatch side effects."""
        return FanOutResult(
            group_id=group_id,
            send_id=uuid.uuid4().hex,
            command=data,
            sent_at=time.time(),
            results=[],
            divergent_sessions=[],
            failed_sessions=[],
            error=error,
        )

    async def _authorized_dispatch_snapshot(
        self,
        group_id: str,
        data: str,
        principal: Principal | None,
    ) -> tuple[FanOutGroup | None, list[str], FanOutResult | None]:
        """Authorize a caller and create the only snapshot accepted by dispatch."""
        if principal is None or isinstance(principal, str):
            return None, [], self._error_result(group_id, data, "authenticated principal required")
        if self._is_global_admin is None or self._resolve_session is None or self._can_read_session is None:
            return None, [], self._error_result(group_id, data, "fan-out authorization is unavailable")
        try:
            is_admin = await self._is_global_admin(principal)
        except Exception:
            return None, [], self._error_result(group_id, data, "fan-out authorization failed")
        if not is_admin:
            return None, [], self._error_result(group_id, data, "global admin role required")

        group = await self._store.get(group_id)
        if group is None:
            return None, [], self._error_result(group_id, data, "fan-out group not found")
        principal_id = self._principal_id(principal)
        if group.created_by != principal_id and principal_id not in group.grants:
            return None, [], self._error_result(group_id, data, "fan-out group not found")

        allowed: list[str] = []
        refused: list[str] = []
        for worker_id in tuple(group.worker_ids):
            try:
                definition = await self._resolve_session(worker_id)
                readable = definition is not None and await self._can_read_session(principal, definition)
            except Exception:
                readable = False
            (allowed if readable else refused).append(worker_id)
        return replace(group, worker_ids=allowed), refused, None

    @staticmethod
    def _strongest_role(principal: Principal) -> str:
        """Return the principal's strongest normalized policy role."""
        for role in ("admin", "operator", "viewer"):
            if role in principal.roles:
                return role
        return "viewer"

    @staticmethod
    def _append_refused(result: FanOutResult, refused: list[str]) -> FanOutResult:
        for worker_id in refused:
            result.results.append(
                SessionFanOutResult(worker_id=worker_id, ok=False, output_delta=None, elapsed_ms=0, divergent=False)
            )
            result.failed_sessions.append(worker_id)
        return result

    # -- Send --------------------------------------------------------------

    async def send(
        self,
        group_id: str,
        data: str,
        *,
        principal: Principal | None,
        quiesce_ms: int | None = None,
        max_response_ms: int | None = None,
    ) -> FanOutResult:
        """Broadcast *data* to all workers in the group and collect results."""
        dispatch_group, refused, auth_error = await self._authorized_dispatch_snapshot(group_id, data, principal)
        if auth_error is not None:
            return auth_error
        if dispatch_group is None:  # pragma: no cover - narrowed by auth_error
            return self._error_result(group_id, data, "fan-out authorization failed")
        group = dispatch_group

        # 1. Check Policy for Fan-Out
        from provide.uterm.server.bridge.hub.approvals import ApprovalRequest, ApprovalStatus
        from provide.uterm.server.bridge.hub.ext import PolicyContext

        # We don't have a WebSocket here necessarily (could be REST),
        # so we pass a dummy or use the principal for context.
        context = PolicyContext(
            worker_id=f"group:{group_id}",
            client_id=principal.subject_id,
            role=self._strongest_role(principal),
            action="fanout_send",
            metadata={"is_fanout": True, "group_id": group_id},
        )

        gate = self._get_fanout_policy_gate()
        decision = await gate.intercept_fanout(data, context, group_id)

        if decision.action == "deny":
            return FanOutResult(
                group_id=group_id,
                send_id=uuid.uuid4().hex,
                command=data,
                sent_at=time.time(),
                results=[],
                divergent_sessions=[],
                failed_sessions=[],
                error=decision.reason or "Command blocked by fan-out policy",
            )

        if decision.action == "hold":
            request_id = uuid.uuid4().hex
            approval = ApprovalRequest(
                id=request_id,
                worker_id=f"group:{group_id}",
                submitter_id=principal.subject_id,
                command=data,
                status=ApprovalStatus.PENDING,
                created_at=time.time(),
                expires_at=time.time() + 300,  # 5 min default
                group_id=group_id,
                is_fanout=True,
            )
            await self._hub.append_event(
                f"group:{group_id}",
                "terminal.fanout.hold",
                {
                    "group_id": group_id,
                    "command": data[:500],
                    "request_id": request_id,
                    "principal": principal.subject_id,
                },
            )
            hub_approvals = getattr(self._hub, "approval_store", None)
            if hub_approvals is None:
                msg = "fan-out approval store is unavailable"
                raise RuntimeError(msg)
            if not hub_approvals.add(approval):
                msg = "fan-out approval request ID collision"
                raise RuntimeError(msg)
            self._pending_approvals[request_id] = {
                "group_id": group_id,
                "command": data,
                "quiesce_ms": quiesce_ms,
                "max_response_ms": max_response_ms,
                "principal": principal,
            }

            return FanOutResult(
                group_id=group_id,
                send_id=request_id,
                command=data,
                sent_at=time.time(),
                results=[],
                divergent_sessions=[],
                failed_sessions=[],
                approval_required=True,
                approval_id=request_id,
            )

        # 2. Standard execution
        q_ms = quiesce_ms if quiesce_ms is not None else group.quiesce_ms
        m_ms = max_response_ms if max_response_ms is not None else group.max_response_ms

        if group.mode == "sequential":
            result = await self._send_sequential(dispatch_group, data, q_ms, m_ms, principal=principal.subject_id)
        else:
            result = await self._send_parallel(dispatch_group, data, q_ms, m_ms, principal=principal.subject_id)
        return self._append_refused(result, refused)

    async def release_approved_command(self, request_id: str) -> FanOutResult | None:
        """Execute a previously held fan-out command after approval."""
        pending = self._pending_approvals.pop(request_id, None)
        if pending is None:
            return None

        group_id = pending["group_id"]
        command = pending["command"]
        principal = pending["principal"]
        q_ms = pending["quiesce_ms"]
        m_ms = pending["max_response_ms"]

        # Note: This executes the send *again* but now with approval bypass logic
        # For simplicity, we just call the underlying send helpers directly.
        dispatch_group, refused, auth_error = await self._authorized_dispatch_snapshot(group_id, command, principal)
        if auth_error is not None:
            return auth_error
        if dispatch_group is None:  # pragma: no cover - narrowed by auth_error
            return self._error_result(group_id, command, "fan-out authorization failed")

        q_ms = q_ms if q_ms is not None else dispatch_group.quiesce_ms
        m_ms = m_ms if m_ms is not None else dispatch_group.max_response_ms

        if dispatch_group.mode == "sequential":
            result = await self._send_sequential(dispatch_group, command, q_ms, m_ms, principal=principal.subject_id)
        else:
            result = await self._send_parallel(dispatch_group, command, q_ms, m_ms, principal=principal.subject_id)
        return self._append_refused(result, refused)

    async def _notify_fanout_observers(self, group: FanOutGroup, data: str, send_id: str, principal: str) -> None:
        """Tell each target session's observers that this input is fan-out-originated,
        so they can distinguish it from a local hijack (ARD multi-session-fanout)."""
        frame = {
            "type": "fanout_input",
            "group_id": group.group_id,
            "send_id": send_id,
            "command": data,
            "from_principal": principal,
        }
        await asyncio.gather(
            *(self._hub.broadcast(wid, frame) for wid in group.worker_ids),
            return_exceptions=True,
        )

    # -- Parallel ----------------------------------------------------------

    async def _send_parallel(
        self,
        group: FanOutGroup,
        data: str,
        quiesce_ms: int,
        max_response_ms: int,
        *,
        principal: str,
    ) -> FanOutResult:
        send_id = uuid.uuid4().hex
        sent_at = time.time()
        frame = {"type": "input", "data": data, "ts": sent_at}
        captures: list[Any | None] = []

        try:
            # Preparation is complete for every member before any notification
            # or input, so synchronous worker output cannot race subscription.
            for worker_id in group.worker_ids:
                try:
                    captures.append(await OutputCollector().open(self._hub, worker_id))
                except Exception:
                    captures.append(None)

            ready_indices = [index for index, capture in enumerate(captures) if capture is not None]
            ready_ids = [group.worker_ids[index] for index in ready_indices]
            ready_group = replace(group, worker_ids=ready_ids)
            if ready_ids:
                await self._notify_fanout_observers(ready_group, data, send_id, principal)

            async def _send(worker_id: str) -> tuple[bool | BaseException, float]:
                try:
                    accepted = await self._hub.send_worker(worker_id, frame)
                    return accepted, time.monotonic()
                except Exception as exc:
                    return exc, time.monotonic()

            dispatched = await asyncio.gather(*(_send(worker_id) for worker_id in ready_ids))
            send_results: list[tuple[bool | BaseException, float] | None] = [None] * len(group.worker_ids)
            for index, dispatched_item in zip(ready_indices, dispatched, strict=True):
                send_results[index] = dispatched_item

            async def _collect(capture: Any, started_at: float) -> tuple[str, int]:
                return await capture.collect(
                    quiesce_ms=quiesce_ms,
                    max_ms=max_response_ms,
                    started_at=started_at,
                )

            collect_indices: list[int] = []
            tasks: list[asyncio.Task[tuple[str, int]]] = []
            for index in ready_indices:
                dispatched_item = send_results[index]
                assert dispatched_item is not None
                accepted, started_at = dispatched_item
                if accepted is True:
                    collect_indices.append(index)
                    tasks.append(asyncio.create_task(_collect(captures[index], started_at)))

            collected_items: list[tuple[str, int] | BaseException] = (
                await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
            )
            collected: list[tuple[str, int] | BaseException | None] = [None] * len(group.worker_ids)
            for index, collected_item in zip(collect_indices, collected_items, strict=True):
                collected[index] = collected_item

            # Build per-session results
            results: list[SessionFanOutResult] = []
            failed_sessions: list[str] = []
            successful_outputs: list[str] = []
            successful_indices: list[int] = []

            for index, worker_id in enumerate(group.worker_ids):
                dispatched_item = send_results[index]
                if dispatched_item is None:
                    item: tuple[str, int] | BaseException | None = None
                else:
                    accepted, _started_at = dispatched_item
                    item = collected[index] if accepted is True else None
                if isinstance(item, tuple):
                    delta, elapsed = item
                    results.append(
                        SessionFanOutResult(
                            worker_id=worker_id,
                            ok=True,
                            output_delta=delta,
                            elapsed_ms=elapsed,
                            divergent=False,
                        )
                    )
                    successful_outputs.append(delta)
                    successful_indices.append(len(results) - 1)
                else:
                    results.append(
                        SessionFanOutResult(
                            worker_id=worker_id,
                            ok=False,
                            output_delta=None,
                            elapsed_ms=0,
                            divergent=False,
                        )
                    )
                    failed_sessions.append(worker_id)

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
        finally:
            if captures:
                await asyncio.gather(
                    *(capture.close() for capture in captures if capture is not None),
                    return_exceptions=True,
                )

    # -- Sequential --------------------------------------------------------

    async def _send_sequential(
        self,
        group: FanOutGroup,
        data: str,
        quiesce_ms: int,
        max_response_ms: int,
        *,
        principal: str,
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
                results.append(
                    SessionFanOutResult(
                        worker_id=wid,
                        ok=False,
                        output_delta=None,
                        elapsed_ms=0,
                        divergent=False,
                    )
                )
                failed_sessions.append(wid)
                continue

            try:
                capture = await OutputCollector().open(self._hub, wid)
            except Exception:
                results.append(
                    SessionFanOutResult(
                        worker_id=wid,
                        ok=False,
                        output_delta=None,
                        elapsed_ms=0,
                        divergent=False,
                    )
                )
                failed_sessions.append(wid)
                continue
            try:
                await self._notify_fanout_observers(replace(group, worker_ids=[wid]), data, send_id, principal)
                ok = await self._hub.send_worker(wid, frame)
                started_at = time.monotonic()
                if not ok:
                    results.append(
                        SessionFanOutResult(
                            worker_id=wid,
                            ok=False,
                            output_delta=None,
                            elapsed_ms=0,
                            divergent=False,
                        )
                    )
                    failed_sessions.append(wid)
                    continue

                delta, elapsed = await capture.collect(
                    quiesce_ms=quiesce_ms,
                    max_ms=max_response_ms,
                    started_at=started_at,
                )
                results.append(
                    SessionFanOutResult(
                        worker_id=wid,
                        ok=True,
                        output_delta=delta,
                        elapsed_ms=elapsed,
                        divergent=False,
                    )
                )
                successful_outputs.append(delta)
                successful_indices.append(len(results) - 1)

                if (
                    group.stop_on_first_error
                    and group.error_pattern is not None
                    and re.search(group.error_pattern, delta)
                ):
                    stopped = True
            except Exception:
                results.append(
                    SessionFanOutResult(
                        worker_id=wid,
                        ok=False,
                        output_delta=None,
                        elapsed_ms=0,
                        divergent=False,
                    )
                )
                failed_sessions.append(wid)
            finally:
                await capture.close()

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
