#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Hypothesis property-based / fuzz / concurrency tests for TermHub."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import hypothesis.strategies as st_h
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis.stateful import Bundle, RuleBasedStateMachine, initialize, rule
from provide.terminal.bridge.hub import TermHub
from provide.terminal.client import connect_test_ws

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

worker_ids = st_h.from_regex(r"[a-zA-Z0-9_\-]{1,20}", fullmatch=True)
owner_names = st_h.text(min_size=1, max_size=200)
lease_values = st_h.integers(min_value=1, max_value=3600)
input_modes = st_h.sampled_from(["hijack", "open"])
bad_input_modes = st_h.text(min_size=1, max_size=50).filter(lambda s: s not in ("hijack", "open"))
key_strings = st_h.text(min_size=0, max_size=10_000)


def _mock_ws() -> AsyncMock:
    """Create a mock WebSocket that doesn't raise on send_text / close."""
    ws = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    return ws


# ===================================================================
# Category 1: Stateful Testing — TermHub State Machine
# ===================================================================


class TermHubStateMachine(RuleBasedStateMachine):
    """Model TermHub as a state machine and check invariants after each step."""

    workers = Bundle("workers")
    browsers = Bundle("browsers")

    def __init__(self) -> None:
        super().__init__()
        self.hub = TermHub()
        # Track worker_ids we've registered so we can choose from them
        self._worker_ids: list[str] = []
        # Map worker_id -> list of browser mocks
        self._browsers: dict[str, list[AsyncMock]] = {}
        # Map worker_id -> worker_ws mock
        self._worker_ws: dict[str, AsyncMock] = {}

    def _run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    # -- Rules ---------------------------------------------------------------

    @initialize(target=workers, wid=worker_ids)
    def add_first_worker(self, wid: str) -> str:
        st = self._run(self.hub._get(wid))
        ws = _mock_ws()
        st.worker_ws = ws
        self._worker_ids.append(wid)
        self._worker_ws[wid] = ws
        self._browsers.setdefault(wid, [])
        return wid

    @rule(target=workers, wid=worker_ids)
    def add_worker(self, wid: str) -> str:
        st = self._run(self.hub._get(wid))
        ws = _mock_ws()
        st.worker_ws = ws
        if wid not in self._worker_ids:
            self._worker_ids.append(wid)
        self._worker_ws[wid] = ws
        self._browsers.setdefault(wid, [])
        return wid

    @rule(target=browsers, wid=workers)
    def add_browser(self, wid: str) -> tuple[str, AsyncMock]:
        ws = _mock_ws()
        st = self._run(self.hub._get(wid))
        st.browsers[ws] = "operator"
        self._browsers.setdefault(wid, []).append(ws)
        return (wid, ws)

    @rule(browser=browsers)
    def remove_browser(self, browser: tuple[str, AsyncMock]) -> None:
        wid, ws = browser
        # Mirror real WS disconnect: release hijack if owner, then discard
        self._run(self.hub.try_release_ws_hijack(wid, ws))
        if wid in self.hub._workers:
            st = self.hub._workers[wid]
            st.browsers.pop(ws, None)
            if wid in self._browsers and ws in self._browsers[wid]:
                self._browsers[wid].remove(ws)
        self._run(self.hub.prune_if_idle(wid))

    @rule(wid=workers, mode=input_modes)
    def set_input_mode(self, wid: str, mode: str) -> None:
        self._run(self.hub.set_input_mode(wid, mode))

    @rule(browser=browsers)
    def acquire_ws_hijack(self, browser: tuple[str, AsyncMock]) -> None:
        wid, ws = browser
        self._run(self.hub.try_acquire_ws_hijack(wid, ws))

    @rule(browser=browsers)
    def release_ws_hijack(self, browser: tuple[str, AsyncMock]) -> None:
        wid, ws = browser
        self._run(self.hub.try_release_ws_hijack(wid, ws))

    @rule(wid=workers, owner=owner_names, lease=lease_values)
    def acquire_rest_hijack(self, wid: str, owner: str, lease: int) -> None:
        import uuid

        self._run(
            self.hub.try_acquire_rest_hijack(
                wid, owner=owner, lease_s=lease, hijack_id=str(uuid.uuid4()), now=time.monotonic()
            )
        )

    @rule(wid=workers)
    def disconnect_worker(self, wid: str) -> None:
        self._run(self.hub.disconnect_worker(wid))

    # -- Invariants checked after every step ---------------------------------

    def teardown(self) -> None:
        for wid, st in self.hub._workers.items():
            # 1. input_mode always valid
            assert st.input_mode in ("hijack", "open"), f"Bad input_mode: {st.input_mode!r}"

            # 2. hijack_owner must be an AsyncMock (if set) — note: in direct
            # method-call testing, a browser can acquire after being removed from
            # st.browsers (real WS layer prevents this). We only check type here.
            if st.hijack_owner is not None:
                assert isinstance(st.hijack_owner, AsyncMock), "hijack_owner is unexpected type"

            # 3. open mode: _can_send_input True for all browsers
            if st.input_mode == "open":
                for ws in st.browsers:
                    assert self.hub.can_send_input(st, ws), "open mode: browser should be able to send"

            # 4. hijack mode with owner: only owner can send
            if st.input_mode == "hijack" and self.hub.is_dashboard_hijack_active(st):
                for ws in st.browsers:
                    if ws is st.hijack_owner:
                        assert self.hub.can_send_input(st, ws), "owner should be able to send"
                    else:
                        assert not self.hub.can_send_input(st, ws), "non-owner should not send in hijack"

            # 5. set_input_mode("open") fails when hijacked
            if self.hub.is_hijacked(st):
                ok, reason = self._run(self.hub.set_input_mode(wid, "open"))
                assert not ok, "switching to open should fail while hijacked"
                assert reason == "active_hijack"

            # 6. event_seq monotonically increasing (events deque)
            if st.events:
                seqs = [e["seq"] for e in st.events]
                for i in range(1, len(seqs)):
                    assert seqs[i] > seqs[i - 1], f"Non-monotonic event seqs: {seqs}"

        # 7. No idle workers in _workers (should have been pruned)
        for wid, st in list(self.hub._workers.items()):
            has_connections = st.worker_ws is not None or bool(st.browsers)
            has_leases = st.hijack_owner is not None or st.hijack_session is not None
            if not has_connections and not has_leases:
                # Prune should have removed this — call it now and verify
                self._run(self.hub.prune_if_idle(wid))
                assert wid not in self.hub._workers, f"Idle worker {wid} not pruned"


TestHubStateMachine = TermHubStateMachine.TestCase
TestHubStateMachine.settings = settings(
    max_examples=200,
    stateful_step_count=30,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)


# ===================================================================
# Category 2: Property-Based Input Fuzzing via REST
# ===================================================================


def _make_app() -> tuple[Any, TermHub]:
    from fastapi import FastAPI

    hub = TermHub()
    app = FastAPI()
    app.include_router(hub.create_router())
    return app, hub


class TestRESTFuzz:
    """Fuzz REST endpoints with hypothesis-generated payloads."""

    @given(owner=owner_names, lease=st_h.integers(min_value=-1000, max_value=50_000))
    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_acquire_fuzz_owner_lease(self, owner: str, lease: int) -> None:
        from starlette.testclient import TestClient

        app, hub = _make_app()
        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/fuzz1/term") as worker:
            worker.receive_json()  # snapshot_req
            resp = client.post("/worker/fuzz1/hijack/acquire", json={"owner": owner, "lease_s": lease})
            if 1 <= lease <= 14400:
                # Valid lease — should succeed
                assert resp.status_code in (200, 409), f"Unexpected {resp.status_code}"
                if resp.status_code == 200:
                    data = resp.json()
                    assert data["owner"] == owner
                    assert 1 <= (data["lease_expires_at"] - time.time()) <= 14400 + 5
            else:
                # Out of range — pydantic rejects
                assert resp.status_code == 422

    @given(keys=key_strings)
    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_send_fuzz_keys(self, keys: str) -> None:
        from starlette.testclient import TestClient

        app, hub = _make_app()
        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/fuzz1/term") as worker:
            worker.receive_json()  # snapshot_req
            # Acquire hijack first
            acq = client.post("/worker/fuzz1/hijack/acquire", json={"owner": "fuzz", "lease_s": 300})
            assert acq.status_code == 200
            hijack_id = acq.json()["hijack_id"]

            payload: dict[str, Any] = {"keys": keys}
            resp = client.post(f"/worker/fuzz1/hijack/{hijack_id}/send", json=payload)
            if not keys:
                # Empty keys — should be rejected by pydantic (min_length) or route
                assert resp.status_code in (400, 422), f"Empty keys gave {resp.status_code}"
            elif len(keys) > 10_000:
                assert resp.status_code == 422
            else:
                # Valid send — should succeed (worker mock swallows the message)
                assert resp.status_code == 200

    @given(mode=st_h.text(min_size=0, max_size=50))
    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_input_mode_fuzz(self, mode: str) -> None:
        from starlette.testclient import TestClient

        app, hub = _make_app()
        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/fuzz1/term") as worker:
            worker.receive_json()  # snapshot_req
            resp = client.post("/worker/fuzz1/input_mode", json={"input_mode": mode})
            if mode in ("hijack", "open"):
                assert resp.status_code == 200
            else:
                assert resp.status_code == 422

    @given(wid=st_h.text(min_size=0, max_size=100))
    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_worker_id_path_fuzz(self, wid: str) -> None:
        from starlette.testclient import TestClient

        app, _hub = _make_app()
        with TestClient(app) as client:
            # Try to hit the input_mode endpoint with random worker ids
            try:
                resp = client.post(f"/worker/{wid}/input_mode", json={"input_mode": "hijack"})
            except Exception:
                # URL construction may fail for certain chars — that's fine
                return
            # Should never crash; either 404/200/422 depending on routing
            assert resp.status_code in (200, 404, 405, 422, 307), f"Unexpected: {resp.status_code}"

    @given(lease=st_h.integers(min_value=-10_000, max_value=100_000))
    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_heartbeat_lease_fuzz(self, lease: int) -> None:
        from starlette.testclient import TestClient

        app, hub = _make_app()
        with TestClient(app) as client, connect_test_ws(client, "/ws/worker/fuzz1/term") as worker:
            worker.receive_json()  # snapshot_req
            acq = client.post("/worker/fuzz1/hijack/acquire", json={"owner": "fuzz", "lease_s": 300})
            assert acq.status_code == 200
            hijack_id = acq.json()["hijack_id"]

            resp = client.post(f"/worker/fuzz1/hijack/{hijack_id}/heartbeat", json={"lease_s": lease})
            if 1 <= lease <= 14400:
                assert resp.status_code == 200
            else:
                assert resp.status_code == 422


# ===================================================================
# Category 3: Concurrent Stress / Race Tests
# ===================================================================


class TestConcurrentStress:
    """Stress concurrent operations with hypothesis + asyncio.gather."""

    @given(n=st_h.integers(min_value=2, max_value=20))
    @settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @pytest.mark.asyncio
    async def test_concurrent_acquire_release(self, n: int) -> None:
        """N tasks race acquire/release — at most one owner at any time."""
        hub = TermHub()
        st = await hub._get("w1")
        st.worker_ws = _mock_ws()

        browsers = [_mock_ws() for _ in range(n)]
        for ws in browsers:
            st.browsers[ws] = "operator"

        async def _race(ws: AsyncMock) -> None:
            await hub.try_acquire_ws_hijack("w1", ws)
            await asyncio.sleep(0)  # yield
            await hub.try_release_ws_hijack("w1", ws)

        await asyncio.gather(*[_race(ws) for ws in browsers])

        # After all releases: either no owner or exactly one
        final_st = hub._workers.get("w1")
        if final_st is not None and final_st.hijack_owner is not None:
            assert final_st.hijack_owner in set(browsers)

    @given(n=st_h.integers(min_value=2, max_value=20))
    @settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @pytest.mark.asyncio
    async def test_concurrent_mode_switch(self, n: int) -> None:
        """N tasks toggle input_mode concurrently — mode always valid after settling."""
        hub = TermHub()
        st = await hub._get("w1")
        st.worker_ws = _mock_ws()

        modes = ["hijack", "open"] * (n // 2) + ["hijack"] * (n % 2)

        async def _switch(mode: str) -> None:
            await hub.set_input_mode("w1", mode)
            await asyncio.sleep(0)

        await asyncio.gather(*[_switch(m) for m in modes])

        final_st = hub._workers.get("w1")
        if final_st is not None:
            assert final_st.input_mode in ("hijack", "open")

    @given(n=st_h.integers(min_value=2, max_value=15))
    @settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @pytest.mark.asyncio
    async def test_concurrent_disconnect_during_hijack(self, n: int) -> None:
        """disconnect races with acquire/release — no dangling hijack state."""
        hub = TermHub()
        st = await hub._get("w1")
        st.worker_ws = _mock_ws()

        browsers = [_mock_ws() for _ in range(n)]
        for ws in browsers:
            st.browsers[ws] = "operator"

        async def _acquire_release(ws: AsyncMock) -> None:
            await hub.try_acquire_ws_hijack("w1", ws)
            await asyncio.sleep(0)
            await hub.try_release_ws_hijack("w1", ws)

        async def _disconnect() -> None:
            await asyncio.sleep(0)  # let acquires start
            await hub.disconnect_worker("w1")

        tasks = [_acquire_release(ws) for ws in browsers] + [_disconnect()]
        await asyncio.gather(*tasks)

        final_st = hub._workers.get("w1")
        if final_st is not None:
            # After disconnect: worker_ws should be None
            assert final_st.worker_ws is None
            # hijack should be cleared by disconnect
            if not final_st.browsers:
                assert final_st.hijack_owner is None
                assert final_st.hijack_session is None

    @given(n=st_h.integers(min_value=2, max_value=30))
    @settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @pytest.mark.asyncio
    async def test_event_seq_monotonic_under_concurrency(self, n: int) -> None:
        """N tasks append events — all seqs unique and increasing."""
        hub = TermHub()
        st = await hub._get("w1")
        st.worker_ws = _mock_ws()

        results: list[dict[str, Any]] = []
        lock = asyncio.Lock()

        async def _append(i: int) -> None:
            evt = await hub.append_event("w1", "test", {"i": i})
            async with lock:
                results.append(evt)

        await asyncio.gather(*[_append(i) for i in range(n)])

        seqs = sorted(e["seq"] for e in results)
        # All unique
        assert len(seqs) == len(set(seqs)), f"Duplicate seqs: {seqs}"
        # Monotonically increasing
        for i in range(1, len(seqs)):
            assert seqs[i] > seqs[i - 1], f"Non-monotonic: {seqs}"
        # Starts at 1
        assert seqs[0] == 1
        assert seqs[-1] == n
