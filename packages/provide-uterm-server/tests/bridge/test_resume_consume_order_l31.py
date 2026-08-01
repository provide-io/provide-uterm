#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regression tests for L31: resume token consumed before validation.

``_handle_resume`` consumed the single-use resume token BEFORE validating the
worker_id match and running the ``_on_resume`` callback. A wrong-worker or
callback-rejected resume therefore BURNED a valid token, so the legitimate
browser could no longer resume.

The fix uses a non-destructive ``get()`` for the validation gates and only
``consume()``s on the success path (preserving atomic single-use semantics).
These tests assert the token survives a rejected resume (a subsequent correct
resume with the same token still succeeds) and is burned on success.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.server.bridge.hub import InMemoryResumeStore, TermHub
from provide.uterm.server.bridge.models import WorkerTermState
from provide.uterm.server.bridge.routes.browser_handlers import _handle_resume, _rollback_reclaimed_hijack


def _make_ws() -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


async def _register(hub: TermHub, worker_id: str, browser_ws: Any, role: str) -> WorkerTermState:
    async with hub._lock:
        st = hub.registry._workers.setdefault(worker_id, WorkerTermState())
        st.browsers[browser_ws] = role
        return st


async def test_wrong_worker_id_does_not_burn_token() -> None:
    """A resume with a mismatched worker_id must leave the token still consumable."""
    store = InMemoryResumeStore()
    hub = TermHub(resume_store=store)

    # Token was minted for worker "right".
    token = await store.create("right", "admin", 300)

    # Browser presents it on the WRONG worker_id → must be rejected.
    ws = _make_ws()
    await _register(hub, "wrong", ws, "admin")
    result = await _handle_resume(hub, ws, "wrong", "admin", {"token": token}, False)
    assert result is False

    # The token must NOT have been consumed by the failed attempt.
    assert await store.get(token) is not None, "wrong-worker resume burned the token"

    # The legitimate browser can now resume on the correct worker_id.
    ws2 = _make_ws()
    await _register(hub, "right", ws2, "admin")
    await _handle_resume(hub, ws2, "right", "admin", {"token": token}, False)
    # After a successful resume the original token is consumed.
    assert await store.get(token) is None


async def test_callback_rejected_does_not_burn_token() -> None:
    """A resume rejected by the _on_resume callback must leave the token consumable."""
    store = InMemoryResumeStore()
    reject_calls: list[str] = []

    async def _reject(_token: str, _session: Any) -> bool:
        reject_calls.append(_token)
        return False

    hub = TermHub(resume_store=store, on_resume=_reject)
    token = await store.create("w1", "admin", 300)

    ws = _make_ws()
    await _register(hub, "w1", ws, "admin")
    result = await _handle_resume(hub, ws, "w1", "admin", {"token": token}, False)
    assert result is False
    assert reject_calls == [token]

    # Callback rejection must NOT burn the token.
    assert await store.get(token) is not None, "callback-rejected resume burned the token"


async def test_successful_resume_consumes_token() -> None:
    """A successful resume must atomically consume the token (replay fails)."""
    store = InMemoryResumeStore()
    hub = TermHub(resume_store=store)
    token = await store.create("w1", "admin", 300)

    ws = _make_ws()
    await _register(hub, "w1", ws, "admin")
    await _handle_resume(hub, ws, "w1", "admin", {"token": token}, False)

    # Token is gone after a successful resume.
    assert await store.get(token) is None
    assert await store.consume(token) is None

    # A replay of the same token on a fresh browser is rejected (no new token minted).
    ws2 = _make_ws()
    await _register(hub, "w1", ws2, "admin")
    before = len(store)
    result = await _handle_resume(hub, ws2, "w1", "admin", {"token": token}, False)
    assert result is False
    assert len(store) == before, "replay of a consumed token must not mint a new token"


async def test_concurrent_consume_race_loser_bails() -> None:
    """If the token passes get() but a concurrent resume consumes it first, bail.

    Covers the success-path consume() returning None: the gates passed on a
    live token, but a racing browser burned it between get() and consume().
    """
    store = InMemoryResumeStore()
    hub = TermHub(resume_store=store)
    token = await store.create("w1", "admin", 300)

    # get() sees a live session, but consume() loses the race (returns None).
    real_get = store.get

    async def _consume_race(_token: str) -> Any:
        return None

    store.consume = _consume_race  # type: ignore[method-assign]
    assert await real_get(token) is not None  # gate would pass

    ws = _make_ws()
    await _register(hub, "w1", ws, "admin")
    before = len(store)
    result = await _handle_resume(hub, ws, "w1", "admin", {"token": token}, False)
    assert result is False
    # No new token minted because we bailed at the consume race.
    assert len(store) == before


async def test_callback_accepted_resume_consumes_token() -> None:
    """When the callback accepts, the token is consumed and a new one is minted."""
    store = InMemoryResumeStore()

    async def _accept(_token: str, _session: Any) -> bool:
        return True

    hub = TermHub(resume_store=store, on_resume=_accept)
    token = await store.create("w1", "admin", 300)

    ws = _make_ws()
    await _register(hub, "w1", ws, "admin")
    await _handle_resume(hub, ws, "w1", "admin", {"token": token}, False)

    # Old token consumed; a fresh token now tracks the ws.
    assert await store.get(token) is None
    new_token = hub._ws_to_resume_token[ws]
    assert new_token != token
    assert await store.get(new_token) is not None


async def test_competing_owner_rejection_preserves_token_and_owner() -> None:
    store = InMemoryResumeStore()
    hub = TermHub(resume_store=store)
    token = await store.create("w1", "admin", 300)
    await store.mark_hijack_owner(token, True)
    resumer = _make_ws()
    competitor = _make_ws()
    worker = _make_ws()
    state = await _register(hub, "w1", resumer, "admin")
    async with hub._lock:
        state.worker_ws = worker
        state.hijack_owner = competitor
        state.hijack_owner_expires_at = time.monotonic() + 60

    result = await _handle_resume(hub, resumer, "w1", "admin", {"token": token}, False)

    assert result is False
    assert await store.get(token) is not None
    assert state.hijack_owner is competitor
    assert resumer not in hub._ws_to_resume_token
    resumer.send_text.assert_not_awaited()
    worker.send_text.assert_not_awaited()


async def test_concurrent_resume_replay_has_exactly_one_winner() -> None:
    class _GatedStore(InMemoryResumeStore):
        def __init__(self) -> None:
            super().__init__()
            self.waiting = 0
            self.both_waiting = asyncio.Event()

        async def consume(self, token: str) -> Any:
            self.waiting += 1
            if self.waiting == 2:
                self.both_waiting.set()
            await self.both_waiting.wait()
            return await super().consume(token)

    store = _GatedStore()
    hub = TermHub(resume_store=store)
    token = await store.create("w1", "admin", 300)
    first = _make_ws()
    second = _make_ws()
    await _register(hub, "w1", first, "admin")
    await _register(hub, "w1", second, "admin")

    await asyncio.gather(
        _handle_resume(hub, first, "w1", "admin", {"token": token}, False),
        _handle_resume(hub, second, "w1", "admin", {"token": token}, False),
    )

    assert await store.get(token) is None
    assert len(hub._ws_to_resume_token) == 1
    assert len(store) == 1
    frames = [call.args[0] for browser in (first, second) for call in browser.send_text.await_args_list]
    assert sum('"type":"hello"' in frame for frame in frames) == 1


async def test_consume_race_after_reclaim_compensates_pause_and_ownership() -> None:
    store = InMemoryResumeStore()
    hub = TermHub(resume_store=store)
    token = await store.create("w1", "admin", 300)
    await store.mark_hijack_owner(token, True)
    resumer = _make_ws()
    worker = _make_ws()
    state = await _register(hub, "w1", resumer, "admin")
    async with hub._lock:
        state.worker_ws = worker

    async def _lose_consume(_token: str) -> None:
        return None

    store.consume = _lose_consume  # type: ignore[method-assign]
    result = await _handle_resume(hub, resumer, "w1", "admin", {"token": token}, False)

    assert result is False
    assert await store.get(token) is not None
    assert state.hijack_owner is None
    actions = [call.args[0] for call in worker.send_text.await_args_list]
    assert "pause" in actions[0]
    assert "resume" in actions[1]


async def test_rollback_never_releases_or_resumes_a_competitor() -> None:
    store = InMemoryResumeStore()
    hub = TermHub(resume_store=store)
    token = await store.create("w1", "admin", 300)
    await store.mark_hijack_owner(token, True)
    resumer = _make_ws()
    competitor = _make_ws()
    worker = _make_ws()
    state = await _register(hub, "w1", resumer, "admin")
    async with hub._lock:
        state.worker_ws = worker

    async def _competitor_wins(_token: str) -> None:
        async with hub._lock:
            state.hijack_owner = competitor
            state.hijack_owner_expires_at = time.monotonic() + 60
            state.ownership_generation += 1

    store.consume = _competitor_wins  # type: ignore[method-assign]
    result = await _handle_resume(hub, resumer, "w1", "admin", {"token": token}, False)

    assert result is False
    assert await store.get(token) is not None
    assert state.hijack_owner is competitor
    assert worker.send_text.await_count == 1
    assert "pause" in worker.send_text.await_args_list[0].args[0]


async def test_consume_exception_revokes_precreated_token_and_preserves_old() -> None:
    store = InMemoryResumeStore()
    hub = TermHub(resume_store=store)
    token = await store.create("w1", "admin", 300)
    ws = _make_ws()
    await _register(hub, "w1", ws, "admin")

    async def _explode(_token: str) -> None:
        raise RuntimeError("consume unavailable")

    store.consume = _explode  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="consume unavailable"):
        await _handle_resume(hub, ws, "w1", "admin", {"token": token}, False)

    assert await store.get(token) is not None
    assert len(store) == 1


async def test_revoke_cleanup_failure_does_not_mask_consume_error() -> None:
    store = InMemoryResumeStore()
    hub = TermHub(resume_store=store)
    token = await store.create("w1", "admin", 300)
    ws = _make_ws()
    await _register(hub, "w1", ws, "admin")

    async def _explode_consume(_token: str) -> None:
        raise RuntimeError("consume unavailable")

    async def _explode_revoke(_token: str) -> None:
        raise RuntimeError("revoke unavailable")

    store.consume = _explode_consume  # type: ignore[method-assign]
    store.revoke = _explode_revoke  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="consume unavailable"):
        await _handle_resume(hub, ws, "w1", "admin", {"token": token}, False)

    assert await store.get(token) is not None


async def test_create_exception_preserves_old_token() -> None:
    store = InMemoryResumeStore()
    hub = TermHub(resume_store=store)
    token = await store.create("w1", "admin", 300)
    ws = _make_ws()
    await _register(hub, "w1", ws, "admin")

    async def _explode_create(_worker_id: str, _role: str, _ttl_s: float) -> str:
        raise RuntimeError("create unavailable")

    store.create = _explode_create  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="create unavailable"):
        await _handle_resume(hub, ws, "w1", "admin", {"token": token}, False)

    assert await store.get(token) is not None
    assert ws not in hub._ws_to_resume_token


async def test_consume_exception_after_reclaim_rolls_back_pause_and_owner() -> None:
    store = InMemoryResumeStore()
    hub = TermHub(resume_store=store)
    token = await store.create("w1", "admin", 300)
    await store.mark_hijack_owner(token, True)
    ws = _make_ws()
    worker = _make_ws()
    state = await _register(hub, "w1", ws, "admin")
    async with hub._lock:
        state.worker_ws = worker

    async def _explode_consume(_token: str) -> None:
        raise RuntimeError("consume unavailable")

    store.consume = _explode_consume  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="consume unavailable"):
        await _handle_resume(hub, ws, "w1", "admin", {"token": token}, False)

    assert await store.get(token) is not None
    assert len(store) == 1
    assert state.hijack_owner is None
    actions = [call.args[0] for call in worker.send_text.await_args_list]
    assert "pause" in actions[0]
    assert "resume" in actions[1]


async def test_rollback_with_active_rest_lease_does_not_resume_worker() -> None:
    hub = TermHub()
    ws = _make_ws()
    hub.try_release_ws_hijack = AsyncMock(return_value=(True, True))  # type: ignore[method-assign]
    hub.send_worker_if_unowned = AsyncMock()  # type: ignore[method-assign]
    hub.broadcast_hijack_state = AsyncMock()  # type: ignore[method-assign]

    await _rollback_reclaimed_hijack(hub, ws, "w1")

    hub.send_worker_if_unowned.assert_not_awaited()
    hub.broadcast_hijack_state.assert_awaited_once_with("w1")
