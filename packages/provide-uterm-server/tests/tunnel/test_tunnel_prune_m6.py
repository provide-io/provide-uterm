#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regression test for M6: tunnel-worker disconnect must prune the hub entry.

The tunnel disconnect ``finally`` called ``deregister_worker`` (which clears
``worker_ws`` and hijack state) but NOT ``prune_if_idle`` — so the stale
``WorkerTermState`` entry lingered in ``hub.registry._workers``. With the new
global ``max_workers`` cap counting every live ``_workers`` entry, dead tunnel
sessions kept consuming capacity and eventually rejected new workers.

The regular ``/ws/worker/{id}/term`` route already prunes on disconnect; the
tunnel route must do the same.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.tunnel.fastapi_routes import register_tunnel_routes as _tunnel_registrar


@pytest.fixture
def hub() -> TermHub:
    return TermHub()


@pytest.fixture
def app(hub: TermHub) -> FastAPI:
    app = FastAPI()
    app.include_router(hub.create_router(extra_route_registrars=[_tunnel_registrar]))
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def test_tunnel_disconnect_prunes_idle_worker_entry(hub: TermHub, client: TestClient) -> None:
    """After a tunnel worker disconnects (no browsers), its _workers entry must be pruned."""
    # Connect and immediately disconnect a tunnel worker with no browsers attached.
    with client.websocket_connect("/tunnel/prune-me"):
        # While connected, the worker entry exists.
        assert "prune-me" in hub.registry._workers

    # After disconnect, the idle entry must be pruned (worker_ws cleared, no
    # browsers, no hijack) so it stops counting against the max_workers cap.
    assert "prune-me" not in hub.registry._workers, "stale tunnel worker entry was not pruned"
    assert len(hub.registry._workers) == 0


def test_tunnel_disconnect_does_not_prune_when_browser_present(hub: TermHub, client: TestClient) -> None:
    """A tunnel disconnect must NOT prune the entry while a browser is still attached.

    prune_if_idle is conservative: it only removes a worker entry when there
    are no browsers and no lease. This guards against the fix over-pruning.
    """
    with client.websocket_connect("/ws/browser/keep-me/term"):
        with client.websocket_connect("/tunnel/keep-me"):
            assert "keep-me" in hub.registry._workers
        # Tunnel gone, but the browser still holds the entry → not pruned.
        assert "keep-me" in hub.registry._workers
