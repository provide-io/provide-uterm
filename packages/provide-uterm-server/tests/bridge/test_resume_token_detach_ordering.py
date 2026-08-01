#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Ordering between a browser's disconnect bookkeeping and its reconnect resume.

A resume token's ``was_hijack_owner`` flag is written by the *disconnecting*
socket's cleanup, which runs on that socket's own task. The reconnecting socket
reads the same flag to decide whether to reclaim the hijack lease. Without an
explicit ordering the reconnect usually won the race, read ``False``, and the
user came back as a plain viewer with every keystroke fenced.

``TermHub`` now arms a detach latch per bound token (``_resume_token_detached``)
and ``_handle_resume`` waits on it before reading the store — a port of the Go
hub's ``resumeTokenDetached`` / ``WaitResumeTokenReady``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.server.bridge.hub import InMemoryResumeStore, TermHub
from provide.uterm.server.bridge.hub import core_impl as hub_core_impl
from provide.uterm.server.bridge.routes.browser_handlers import _handle_resume


def _ws() -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    ws.state = MagicMock()
    ws.state.uterm_principal = None
    return ws


async def _settle(times: int = 25) -> None:
    """Yield to the loop enough times for any *unordered* task to finish."""
    for _ in range(times):
        await asyncio.sleep(0)


async def _hub_with_owner(worker_id: str = "w1") -> tuple[TermHub, InMemoryResumeStore, Any, Any, str]:
    """Build a hub whose ``old_ws`` browser holds the dashboard hijack lease."""
    store = InMemoryResumeStore()
    hub = TermHub(resume_store=store)
    worker = _ws()
    await hub.register_worker(worker_id, worker)
    old_ws = _ws()
    old_state = await hub.register_browser(worker_id, old_ws, "admin")
    old_token = old_state["resume_token"]
    assert isinstance(old_token, str)
    reclaimed, _competing = await hub.try_reclaim_hijack_status(worker_id, old_ws)
    assert reclaimed is True
    return hub, store, worker, old_ws, old_token


class TestReconnectOrdering:
    """The regression: a reconnect must not outrun the disconnect bookkeeping."""

    async def test_reconnect_reclaims_while_disconnect_bookkeeping_is_in_flight(self) -> None:
        """A resume issued mid-disconnect waits, then reclaims the lease.

        RED before the fix: the resume task runs straight through while the old
        socket's ``mark_hijack_owner`` is still pending, reads
        ``was_hijack_owner=False``, and returns ``False`` (plain viewer).
        """
        hub, store, _worker, old_ws, old_token = await _hub_with_owner()
        new_ws = _ws()
        await hub.register_browser("w1", new_ws, "admin")

        entered = asyncio.Event()
        release = asyncio.Event()
        real_mark = store.mark_hijack_owner

        async def gated_mark(token: str, is_owner: bool) -> None:
            entered.set()
            await release.wait()
            await real_mark(token, is_owner)

        store.mark_hijack_owner = gated_mark  # type: ignore[method-assign]

        disconnect = asyncio.create_task(hub.cleanup_browser_disconnect("w1", old_ws, True))
        await asyncio.wait_for(entered.wait(), 2.0)

        resume = asyncio.create_task(_handle_resume(hub, new_ws, "w1", "admin", {"token": old_token}, False))
        await _settle()
        # The ordering assertion: the resume cannot have decided anything yet.
        assert not resume.done(), "resume read the resume store before the disconnect bookkeeping committed"

        release.set()
        await asyncio.wait_for(disconnect, 2.0)
        owned_hijack = await asyncio.wait_for(resume, 2.0)

        assert owned_hijack is True
        state = hub.registry.get("w1")
        assert state is not None
        assert state.hijack_owner is new_ws

    async def test_reconnect_after_completed_disconnect_still_reclaims(self) -> None:
        """The already-detached case takes the fast path and still reclaims."""
        hub, _store, _worker, old_ws, old_token = await _hub_with_owner()
        await hub.cleanup_browser_disconnect("w1", old_ws, True)
        assert old_token not in hub._resume_token_detached

        new_ws = _ws()
        await hub.register_browser("w1", new_ws, "admin")
        owned_hijack = await _handle_resume(hub, new_ws, "w1", "admin", {"token": old_token}, False)

        assert owned_hijack is True


class TestWaitResumeTokenReady:
    """Direct unit tests for the latch primitive itself."""

    async def test_returns_true_for_token_still_bound_to_the_same_socket(self) -> None:
        """A socket resuming with its OWN live token has nothing to wait for."""
        hub = TermHub(resume_store=InMemoryResumeStore())
        ws = _ws()
        state = await hub.register_browser("w1", ws, "admin")
        token = state["resume_token"]
        assert isinstance(token, str)
        # The latch is armed and unset, so a wait would block if it were taken.
        assert not hub._resume_token_detached[token].is_set()
        assert await asyncio.wait_for(hub.wait_resume_token_ready(token, ws), 1.0) is True

    async def test_returns_true_for_a_token_with_no_latch(self) -> None:
        """An unknown / already-detached token is ready immediately."""
        hub = TermHub(resume_store=InMemoryResumeStore())
        assert await asyncio.wait_for(hub.wait_resume_token_ready("no-such-token", _ws()), 1.0) is True

    async def test_blocks_until_the_token_is_detached(self) -> None:
        """The wait is released by the detach, not by a timer."""
        hub = TermHub(resume_store=InMemoryResumeStore())
        old_ws = _ws()
        new_ws = _ws()
        state = await hub.register_browser("w1", old_ws, "admin")
        token = state["resume_token"]
        assert isinstance(token, str)
        await hub.register_browser("w1", new_ws, "admin")

        waiter = asyncio.create_task(hub.wait_resume_token_ready(token, new_ws))
        await _settle()
        assert not waiter.done()

        async with hub._lock:
            hub._detach_resume_token_locked(token)
        assert await asyncio.wait_for(waiter, 1.0) is True

    async def test_returns_false_when_the_latch_is_never_released(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A lost latch expires the bound instead of hanging the socket."""
        monkeypatch.setattr(hub_core_impl, "RESUME_TOKEN_DETACH_TIMEOUT_S", 0.02)
        hub = TermHub(resume_store=InMemoryResumeStore())
        old_ws = _ws()
        new_ws = _ws()
        state = await hub.register_browser("w1", old_ws, "admin")
        token = state["resume_token"]
        assert isinstance(token, str)
        await hub.register_browser("w1", new_ws, "admin")

        assert await asyncio.wait_for(hub.wait_resume_token_ready(token, new_ws), 2.0) is False

    async def test_detach_tolerates_none_and_unknown_tokens(self) -> None:
        """``None`` (nothing bound) and an unknown token are both no-ops."""
        hub = TermHub(resume_store=InMemoryResumeStore())
        async with hub._lock:
            hub._detach_resume_token_locked(None)
            hub._detach_resume_token_locked("never-bound")
        assert hub._resume_token_detached == {}


class TestResumeTimeoutFallback:
    """An expired wait proceeds with whatever the store says — it never fails."""

    async def test_resume_proceeds_after_the_wait_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Old socket still connected (latch armed) -> wait expires -> resume runs."""
        monkeypatch.setattr(hub_core_impl, "RESUME_TOKEN_DETACH_TIMEOUT_S", 0.02)
        hub, store, _worker, _old_ws, old_token = await _hub_with_owner()
        # Commit the ownership flag WITHOUT detaching, so only the latch is lost.
        await store.mark_hijack_owner(old_token, True)
        async with hub._lock:
            state = hub.registry.get("w1")
            assert state is not None
            state.hijack_owner = None
            state.hijack_owner_expires_at = None

        new_ws = _ws()
        await hub.register_browser("w1", new_ws, "admin")
        owned_hijack = await asyncio.wait_for(
            _handle_resume(hub, new_ws, "w1", "admin", {"token": old_token}, False), 5.0
        )

        assert owned_hijack is True


class TestTokenBindingLifecycle:
    """Every bind arms a latch; every terminal path releases one."""

    async def test_register_browser_arms_the_latch(self) -> None:
        hub = TermHub(resume_store=InMemoryResumeStore())
        ws = _ws()
        state = await hub.register_browser("w1", ws, "viewer")
        token = state["resume_token"]
        assert isinstance(token, str)
        assert hub._ws_to_resume_token[ws] == token
        assert not hub._resume_token_detached[token].is_set()

    async def test_resume_rebinding_releases_the_superseded_latch(self) -> None:
        """The connect-time token's latch must not be stranded by a resume."""
        hub, _store, _worker, old_ws, old_token = await _hub_with_owner()
        await hub.cleanup_browser_disconnect("w1", old_ws, True)

        new_ws = _ws()
        connect_state = await hub.register_browser("w1", new_ws, "admin")
        connect_token = connect_state["resume_token"]
        assert isinstance(connect_token, str)

        await _handle_resume(hub, new_ws, "w1", "admin", {"token": old_token}, False)

        replacement = hub._ws_to_resume_token[new_ws]
        assert replacement != connect_token
        assert connect_token not in hub._resume_token_detached
        assert not hub._resume_token_detached[replacement].is_set()

    async def test_disconnect_releases_the_latch_even_with_nothing_to_mark(self) -> None:
        """A plain viewer disconnect marks nothing but must still detach.

        Otherwise a later reconnect would block for the full timeout.
        """
        hub = TermHub(resume_store=InMemoryResumeStore())
        worker = _ws()
        await hub.register_worker("w1", worker)
        ws = _ws()
        state = await hub.register_browser("w1", ws, "viewer")
        token = state["resume_token"]
        assert isinstance(token, str)

        await hub.cleanup_browser_disconnect("w1", ws, False)

        assert token not in hub._resume_token_detached
        assert ws not in hub._ws_to_resume_token

    async def test_register_rollback_releases_the_latch(self) -> None:
        """A raise after the token is minted must not strand its latch."""
        store = InMemoryResumeStore()
        hub = TermHub(resume_store=store)
        ws = _ws()
        ws.state.uterm_principal = MagicMock(subject_id="alice")

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("setup exploded")

        # Raises while building initial_state — after the token is bound.
        hub.is_hijacked = _boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="setup exploded"):
            await hub.register_browser("w1", ws, "admin")

        assert hub._resume_token_detached == {}
        assert hub._ws_to_resume_token == {}
