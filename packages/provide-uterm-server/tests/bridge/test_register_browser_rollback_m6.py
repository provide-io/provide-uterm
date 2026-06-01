#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""M6 (verify): ``register_browser`` must roll back the per-principal quota on raise.

The route handler calls ``register_browser`` OUTSIDE its own try/finally
(``websockets_impl.ws_browser_term`` ~line 389). So if ``register_browser``
ITSELF raises after it has already incremented
``hub._principal_browser_counts[subject_id]`` (e.g. ``resume_store.create()``
throws an sqlite IO error / CancelledError), the route's disconnect cleanup
never runs for this socket and the increment is leaked permanently — no reaper
exists for that counter. After ``max_connections_per_principal`` such leaks the
principal is rejected forever with 1008.

These tests pin the rollback at the ``register_browser`` UNIT boundary: a raise
inside the method must leave the per-principal count and ``_ws_principal`` map
exactly as they were before the call.
"""

from __future__ import annotations

from typing import Any

import pytest

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.hub.resume import InMemoryResumeStore
from provide.uterm.server.bridge.identity import Principal


class _PrincipalWS:
    """Minimal WS stand-in carrying a non-anonymous principal for quota counting."""

    def __init__(self, subject_id: str) -> None:
        self.state = type(
            "S",
            (),
            {"uterm_principal": Principal(subject_id=subject_id, roles=frozenset({"admin"}), scopes=frozenset())},
        )()


def _hub_with_resume() -> TermHub:
    return TermHub(max_connections_per_principal=2, resume_store=InMemoryResumeStore())


@pytest.mark.asyncio
async def test_register_browser_rolls_back_count_when_resume_create_raises() -> None:
    """If ``resume_store.create`` raises, the quota increment is rolled back."""
    hub = _hub_with_resume()
    ws = _PrincipalWS("alice")

    async def _boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("simulated sqlite IO error")

    hub._resume_store.create = _boom  # type: ignore[union-attr,method-assign]

    assert hub._principal_browser_counts.get("alice", 0) == 0
    with pytest.raises(RuntimeError, match="simulated sqlite IO error"):
        await hub.register_browser("w1", ws, "viewer")

    # Rolled back: count back to 0, ws not tracked.
    assert hub._principal_browser_counts.get("alice", 0) == 0, "quota leaked on resume-create failure"
    assert ws not in hub._ws_principal


@pytest.mark.asyncio
async def test_register_browser_no_lockout_after_repeated_create_failures() -> None:
    """Repeated ``register_browser`` raises must not accumulate the quota (no lockout)."""
    hub = _hub_with_resume()

    async def _boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("boom")

    hub._resume_store.create = _boom  # type: ignore[union-attr,method-assign]

    # Far more failures than the cap of 2 — a leak would lock alice out.
    for _ in range(5):
        ws = _PrincipalWS("alice")
        with pytest.raises(RuntimeError):
            await hub.register_browser("w1", ws, "viewer")

    assert hub._principal_browser_counts.get("alice", 0) == 0, "repeated failures must not accumulate quota"

    # And a subsequent successful register still works (not spuriously rejected).
    ok_ws = _PrincipalWS("alice")
    hub._resume_store.create = InMemoryResumeStore().create  # type: ignore[union-attr,method-assign]
    state = await hub.register_browser("w1", ok_ws, "viewer")
    assert hub._principal_browser_counts.get("alice", 0) == 1
    assert state["resume_token"] is not None


@pytest.mark.asyncio
async def test_register_browser_rollback_leaves_other_connections_counted() -> None:
    """A raise on the SECOND connection rolls back only its own slot (remaining>0 branch)."""
    hub = _hub_with_resume()

    # First connection succeeds → count is 1.
    ws1 = _PrincipalWS("carol")
    await hub.register_browser("w1", ws1, "viewer")
    assert hub._principal_browser_counts.get("carol", 0) == 1

    # Second connection raises in create → its increment (to 2) is rolled back to 1,
    # NOT popped to 0 — the first connection must remain counted.
    async def _boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("boom")

    hub._resume_store.create = _boom  # type: ignore[union-attr,method-assign]
    ws2 = _PrincipalWS("carol")
    with pytest.raises(RuntimeError):
        await hub.register_browser("w1", ws2, "viewer")

    assert hub._principal_browser_counts.get("carol", 0) == 1, "rollback must not drop the surviving connection"
    assert ws2 not in hub._ws_principal
    assert hub._ws_principal[ws1] == "carol"


@pytest.mark.asyncio
async def test_register_browser_rollback_exempt_principal_create_raise() -> None:
    """An exempt (no-principal) ws raising in create rolls back cleanly (subject_id None branch)."""

    class _AnonWS:
        pass  # no .state → exempt from quota counting

    hub = _hub_with_resume()

    async def _boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("boom")

    hub._resume_store.create = _boom  # type: ignore[union-attr,method-assign]
    ws = _AnonWS()
    with pytest.raises(RuntimeError):
        await hub.register_browser("w1", ws, "viewer")

    # Exempt ws was never counted; nothing to decrement, and no resume token orphaned.
    assert hub._principal_browser_counts == {}
    assert ws not in hub._ws_principal
    assert ws not in hub._ws_to_resume_token


@pytest.mark.asyncio
async def test_register_browser_happy_path_increments_then_disconnect_decrements() -> None:
    """Happy path increments exactly once; a clean disconnect decrements to 0."""
    hub = _hub_with_resume()
    ws = _PrincipalWS("bob")

    state = await hub.register_browser("w1", ws, "viewer")
    assert hub._principal_browser_counts.get("bob", 0) == 1
    assert hub._ws_principal[ws] == "bob"
    assert state["resume_token"] is not None

    await hub.cleanup_browser_disconnect("w1", ws, owned_hijack=False)
    assert hub._principal_browser_counts.get("bob", 0) == 0
    assert ws not in hub._ws_principal
