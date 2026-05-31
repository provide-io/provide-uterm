#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regression tests for L30: orphaned resume token on quota rejection.

``register_browser`` minted a resume token BEFORE the per-principal quota
gate. When the quota rejected (WebSocketException 1008), the just-created
token was orphaned in the resume store (cleaned only by TTL/retention).

The fix mints the resume token only AFTER the quota gate passes, so a
rejected registration leaves the store unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketException

from provide.uterm.server.bridge.hub import InMemoryResumeStore, TermHub
from provide.uterm.server.bridge.identity import Principal
from provide.uterm.server.bridge.models import WorkerTermState


def _make_ws(subject_id: str) -> MagicMock:
    ws = MagicMock()
    ws.send_text = AsyncMock()
    ws.state = SimpleNamespace()
    ws.state.uterm_principal = Principal(
        subject_id=subject_id,
        roles=frozenset({"viewer"}),
        scopes=frozenset(),
    )
    return ws


async def _register(hub: TermHub, ws: MagicMock, worker_id: str = "w1") -> None:
    async with hub._lock:
        hub._workers.setdefault(worker_id, WorkerTermState())
    await hub.register_browser(worker_id, ws, "viewer")


async def test_rejected_register_leaves_no_resume_token() -> None:
    """A quota-rejected register_browser must NOT mint a resume token."""
    store = InMemoryResumeStore()
    hub = TermHub(max_connections_per_principal=1, resume_store=store)

    # First connection succeeds and mints exactly one token.
    await _register(hub, _make_ws("alice"))
    assert len(store) == 1

    # Second connection for alice exceeds the cap → rejected.
    rejected_ws = _make_ws("alice")
    before = len(store)
    with pytest.raises(WebSocketException) as exc_info:
        await _register(hub, rejected_ws)
    assert exc_info.value.code == 1008

    # No new (orphaned) token created by the rejected attempt.
    assert len(store) == before, "rejected register_browser leaked a resume token"
    # The rejected ws is not tracked in the ws→token map either.
    assert rejected_ws not in hub._ws_to_resume_token


async def test_successful_register_mints_exactly_one_token() -> None:
    """A successful register_browser mints exactly one resume token and tracks it."""
    store = InMemoryResumeStore()
    hub = TermHub(max_connections_per_principal=5, resume_store=store)

    ws = _make_ws("bob")
    assert len(store) == 0
    result = await _register_returning(hub, ws)

    assert len(store) == 1
    token = result["resume_token"]
    assert token is not None
    assert hub._ws_to_resume_token[ws] == token
    # The minted token is retrievable from the store.
    assert await store.get(token) is not None


async def _register_returning(hub: TermHub, ws: MagicMock, worker_id: str = "w1") -> dict:
    async with hub._lock:
        hub._workers.setdefault(worker_id, WorkerTermState())
    return await hub.register_browser(worker_id, ws, "viewer")
