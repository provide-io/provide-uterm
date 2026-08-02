#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Dead-socket resume when the worker send never completes.

``remove_dead_browsers`` clears a dead hijack owner and then tries to tell the
worker to resume via ``send_worker_if_unowned``.  That send is bounded by
``_OWNED_INPUT_SEND_TIMEOUT_S``; when the worker socket is wedged the send times
out and returns ``False``, and the hub must NOT announce a hijack-state change
it never actually delivered.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.hub import lease as lease_mod


class _WedgedWorker:
    """Worker socket whose ``send_text`` never returns."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def send_text(self, _payload: str) -> None:
        self.entered.set()
        await asyncio.Event().wait()


async def test_dead_socket_resume_send_timeout_does_not_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    hub = TermHub()
    worker = _WedgedWorker()
    worker_id = "dead-socket-send-timeout"
    owner = AsyncMock()
    await hub.register_worker(worker_id, worker)  # type: ignore[arg-type]
    await hub.register_browser(worker_id, owner, "admin")
    assert await hub.try_acquire_ws_hijack(worker_id, owner) == (True, None)

    notified: list[tuple[str, bool, str | None]] = []
    monkeypatch.setattr(
        hub,
        "notify_hijack_changed",
        lambda wid, enabled, owner: notified.append((wid, enabled, owner)),
    )
    monkeypatch.setattr(lease_mod, "_OWNED_INPUT_SEND_TIMEOUT_S", 0.01)

    # The owner socket is dead: ownership is cleared, so the hub tries to resume
    # the worker — but the worker send wedges and the bounded wait times out.
    assert await hub.remove_dead_browsers(worker_id, {owner}) is True
    assert worker.entered.is_set()
    assert notified == []
