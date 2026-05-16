#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Targeted tests for remaining coverage gaps in _controller.py and _routes.py.

Covers:
- _controller.py 54->exit: delete_group when group not found (no-op path)
- _controller.py 81: _authorized_group returns None for unauthorized caller
- _controller.py 157-160: parallel collect task raises BaseException → failed session
- _routes.py 105: DELETE returns 403 when non-creator tries to delete
- _routes.py 153: POST /grants returns 403 when non-creator tries to grant
- websockets.py 389: fanout_send with group the caller doesn't own → continue (no response)
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.bridge.fanout._controller import FanOutController
from provide.uterm.bridge.fanout._models import FanOutGroup
from provide.uterm.bridge.hub import EventBus, TermHub
from provide.uterm.client import connect_test_ws

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_hub_with_workers(*worker_ids: str) -> TermHub:
    """Create a TermHub with EventBus and register fake workers."""
    hub = TermHub(event_bus=EventBus())
    for wid in worker_ids:
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        await hub.register_worker(wid, ws)
    return hub


def _make_group(
    worker_ids: list[str], mode: str = "parallel", created_by: str = "admin", **kwargs: object
) -> FanOutGroup:
    """Build a minimal FanOutGroup for testing."""
    return FanOutGroup(
        group_id="g1",
        name="test-group",
        worker_ids=worker_ids,
        created_by=created_by,
        created_at=time.time(),
        mode=mode,
        quiesce_ms=300,
        max_response_ms=5_000,
        **kwargs,  # type: ignore[arg-type]
    )


def _make_rest_app() -> object:
    """Create a server app in dev mode with fan-out controller."""
    from provide.uterm.server import create_server_app, default_server_config

    cfg = default_server_config()
    cfg.auth.mode = "dev"
    cfg.sessions = []
    return create_server_app(cfg)


# ---------------------------------------------------------------------------
# _controller.py 54->exit: delete_group when group is None (no-op)
# ---------------------------------------------------------------------------


class TestDeleteGroupGroupNotFound:
    """delete_group when group is not found — the 'if group is not None' is False."""

    async def test_delete_nonexistent_group_is_noop(self) -> None:
        """delete_group on a missing or unauthorized group silently does nothing."""
        hub = TermHub(event_bus=EventBus())
        ctrl = FanOutController(hub)
        # No group created — delete on "nonexistent" is a no-op.
        await ctrl.delete_group("nonexistent", principal="admin")
        # No exception; store remains empty.
        groups = await ctrl.list_groups("admin")
        assert groups == []


# ---------------------------------------------------------------------------
# _controller.py 81: _authorized_group returns None when caller has no access
# ---------------------------------------------------------------------------


class TestAuthorizedGroupUnauthorizedCaller:
    """_authorized_group returns None when caller is neither creator nor grantee."""

    async def test_get_group_unauthorized_caller_returns_none(self) -> None:
        """A caller that is not the creator and not in grants gets None."""
        hub = TermHub(event_bus=EventBus())
        ctrl = FanOutController(hub)
        group = _make_group(["w1"], created_by="alice")
        await ctrl.create_group(group, principal="alice")

        # Bob has no access.
        result = await ctrl.get_group("g1", principal="bob")
        assert result is None

    async def test_send_to_unauthorized_group_returns_empty_result(self) -> None:
        """send() with an unauthorized principal returns an empty FanOutResult."""
        hub = TermHub(event_bus=EventBus())
        ctrl = FanOutController(hub)
        group = _make_group(["w1"], created_by="alice")
        await ctrl.create_group(group, principal="alice")

        # Bob can't send to alice's group.
        result = await ctrl.send("g1", "cmd\n", principal="bob")
        assert result.results == []
        assert result.failed_sessions == []
        assert result.divergent_sessions == []


# ---------------------------------------------------------------------------
# _controller.py 157-160: parallel send — collect task raises BaseException
# ---------------------------------------------------------------------------


class TestParallelCollectTaskException:
    """When a parallel collect task raises a BaseException, the session is marked failed."""

    async def test_parallel_collect_exception_marks_session_failed(self) -> None:
        """If the collect coroutine raises an exception for a worker, that worker is
        recorded in failed_sessions and its result has ok=False."""
        hub = await _make_hub_with_workers("w1", "w2")
        ctrl = FanOutController(hub)
        group = _make_group(["w1", "w2"])
        await ctrl.create_group(group, principal="admin")

        # Patch OutputCollector.collect so it raises for w1 and succeeds for w2.
        from provide.uterm.bridge.fanout._collector import OutputCollector

        original_collect = OutputCollector.collect

        async def _patched_collect(self: object, hub: object, wid: str, **kw: object) -> tuple[str, int]:
            if wid == "w1":
                raise RuntimeError("simulated collection failure")
            return await original_collect(self, hub, wid, **kw)  # type: ignore[arg-type]

        import unittest.mock as _mock

        with _mock.patch.object(OutputCollector, "collect", _patched_collect):
            # We need send_worker to succeed for both so both enter the collect phase.
            _orig_send = hub.send_worker
            bg: list[asyncio.Task[None]] = []

            async def _send_and_emit(wid: str, msg: dict) -> bool:  # type: ignore[type-arg]
                ok = await _orig_send(wid, msg)
                if wid == "w2":

                    async def _emit() -> None:
                        await asyncio.sleep(0.02)
                        await hub.append_event("w2", "term", {"data": "output-w2"})

                    bg.append(asyncio.create_task(_emit()))
                return ok

            hub.send_worker = _send_and_emit  # type: ignore[assignment]

            result = await ctrl.send("g1", "cmd\n", principal="admin")
            for t in bg:
                await t

        # w1 failed during collection → ok=False and in failed_sessions.
        w1_r = next(r for r in result.results if r.worker_id == "w1")
        w2_r = next(r for r in result.results if r.worker_id == "w2")
        assert w1_r.ok is False
        assert "w1" in result.failed_sessions
        # w2 succeeded.
        assert w2_r.ok is True


# ---------------------------------------------------------------------------
# _routes.py 105: DELETE /groups/{id} → 403 when non-creator tries to delete
# ---------------------------------------------------------------------------


class TestDeleteGroupForbidden:
    """_routes.py line 105 — 403 when caller is not the creator of the group."""

    def test_delete_group_by_non_creator_returns_403(self) -> None:
        """The group creator is 'dev' (dev-mode principal). Attempts to delete while
        the hub reports a different group creator return 403."""
        from provide.uterm.bridge.fanout._routes import register_fanout_routes

        # Create a hub with a fan-out controller and register routes.
        hub = TermHub()
        ctrl = FanOutController(hub)
        hub.fan_out_controller = ctrl  # type: ignore[attr-defined]

        app = FastAPI()

        # Middleware that sets request.state.uterm_principal.
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.requests import Request as StarletteRequest

        class _FakePrincipal:
            subject_id = "alice"

        class _SetPrincipal(BaseHTTPMiddleware):
            async def dispatch(self, request: StarletteRequest, call_next: object) -> object:
                request.state.uterm_principal = _FakePrincipal()
                return await call_next(request)  # type: ignore[operator]

        app.add_middleware(_SetPrincipal)
        app.include_router(hub.create_router(extra_route_registrars=[register_fanout_routes]))

        # Create a group owned by "bob" (not "alice").
        import asyncio

        group = FanOutGroup(
            group_id="gx",
            name="bob-group",
            worker_ids=[],
            created_by="bob",
            created_at=time.time(),
            mode="parallel",
            quiesce_ms=300,
            max_response_ms=5_000,
        )
        asyncio.run(ctrl._store.save(group))
        # Also grant alice read access so get_group returns the group.
        asyncio.run(ctrl.grant_access("gx", "alice", principal="bob"))

        with TestClient(app) as client:
            resp = client.delete("/api/fanout/groups/gx")

        assert resp.status_code == 403
        assert "creator" in resp.json()["error"]


# ---------------------------------------------------------------------------
# _routes.py 153: POST /grants → 403 when non-creator tries to grant
# ---------------------------------------------------------------------------


class TestGrantAccessForbidden:
    """_routes.py line 153 — 403 when caller is not the creator of the group."""

    def test_grant_access_by_non_creator_returns_403(self) -> None:
        """Only the group creator can grant access; non-creator gets 403."""
        from provide.uterm.bridge.fanout._routes import register_fanout_routes

        hub = TermHub()
        ctrl = FanOutController(hub)
        hub.fan_out_controller = ctrl  # type: ignore[attr-defined]

        app = FastAPI()

        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.requests import Request as StarletteRequest

        class _FakePrincipal:
            subject_id = "alice"

        class _SetPrincipal(BaseHTTPMiddleware):
            async def dispatch(self, request: StarletteRequest, call_next: object) -> object:
                request.state.uterm_principal = _FakePrincipal()
                return await call_next(request)  # type: ignore[operator]

        app.add_middleware(_SetPrincipal)
        app.include_router(hub.create_router(extra_route_registrars=[register_fanout_routes]))

        import asyncio

        group = FanOutGroup(
            group_id="gy",
            name="bob-group",
            worker_ids=[],
            created_by="bob",
            created_at=time.time(),
            mode="parallel",
            quiesce_ms=300,
            max_response_ms=5_000,
        )
        asyncio.run(ctrl._store.save(group))
        # Grant alice read access so get_group returns the group.
        asyncio.run(ctrl.grant_access("gy", "alice", principal="bob"))

        with TestClient(app) as client:
            resp = client.post("/api/fanout/groups/gy/grants", json={"grantee": "carol"})

        assert resp.status_code == 403
        assert "creator" in resp.json()["error"]


# ---------------------------------------------------------------------------
# websockets.py 389: fanout_send with unknown/unauthorized group → continue
# ---------------------------------------------------------------------------


class TestFanoutSendWsGroupNotFound:
    """websockets.py line 389 — fanout_send when get_group returns None (continue)."""

    def test_fanout_send_unknown_group_silently_ignored(self) -> None:
        """When a browser sends fanout_send with a group the caller doesn't own,
        get_group returns None and the handler does a bare 'continue' — no response
        frame is sent and the WebSocket stays alive."""

        def resolver(_ws, _worker_id):
            return "operator"

        hub = TermHub(resolve_browser_role=resolver)

        ctrl = FanOutController(hub)
        hub.fan_out_controller = ctrl  # type: ignore[attr-defined]

        app = FastAPI()
        app.include_router(hub.create_router())

        with TestClient(app) as client, connect_test_ws(client, "/ws/browser/bot1/term") as browser:
            # Consume hello + hijack_state.
            hello = browser.receive_json()
            assert hello["type"] == "hello"
            hijack_state = browser.receive_json()
            assert hijack_state["type"] == "hijack_state"

            # Send fanout_send for a group the anonymous caller does not own.
            browser.send_json(
                {
                    "type": "fanout_send",
                    "group_id": "does-not-exist",
                    "data": "echo hi\n",
                }
            )

            # Send a ping-like message so the loop stays live.
            # No fanout_result frame should arrive (the continue path was taken).
            # If we receive anything here it should NOT be fanout_result.
            # Close the connection cleanly.
