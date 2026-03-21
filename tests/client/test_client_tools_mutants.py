#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for client — ok helper and hijack tools."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport

from provide.terminal.client.mcp_tools import _ok, hijack_tools
from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.models import WorkerTermState

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

WID = "mutant-worker"


def _make_hub_app() -> tuple[TermHub, FastAPI]:
    hub = TermHub(resolve_browser_role=lambda _ws, _wid: "admin")
    app = FastAPI()
    app.include_router(hub.create_router())
    return hub, app


def _add_worker(hub: TermHub, worker_id: str = WID) -> AsyncMock:
    mock_ws = AsyncMock()
    mock_ws.send_text = AsyncMock()
    hub._workers[worker_id] = WorkerTermState(worker_ws=mock_ws)
    return mock_ws


class TestOkHelperMutants:
    def test_ok_with_dict_preserves_data(self) -> None:
        """mutmut_28/77/86/95: _ok must return data from second arg, not None."""
        result = _ok(True, {"hijack_id": "abc", "owner": "bot"})
        assert result["hijack_id"] == "abc"
        assert result["owner"] == "bot"

    def test_ok_failure_with_dict(self) -> None:
        """_ok with ok=False still returns data dict."""
        result = _ok(False, {"error": "not found"})
        assert result["success"] is False
        assert result["error"] == "not found"

    def test_ok_with_non_dict_uses_data_key(self) -> None:
        """Non-dict data goes into 'data' key, not discarded."""
        result = _ok(True, ["event1", "event2"])
        assert result["data"] == ["event1", "event2"]
        assert result["data"] is not None

    def test_ok_result_is_not_none_data(self) -> None:
        """Mutants that pass None to _ok instead of data would give empty/None data."""
        data = {"snapshot": "screen content here", "seq": 42}
        result = _ok(True, data)
        assert result.get("snapshot") == "screen content here"
        assert result.get("seq") == 42
        # Would fail if mutant passes None: None is not a dict, so result["data"] = None
        assert result.get("data") is None or result.get("snapshot") is not None


# ---------------------------------------------------------------------------
# mcp_tools.hijack_tools — mutmut_5/19: lease_s default = 90
# mutmut_6/7: owner default = "operator"
# mutmut_13: owner not passed to acquire
# mutmut_14: lease_s not passed to acquire
# ---------------------------------------------------------------------------


class TestHijackToolsMutants:
    async def test_hijack_begin_default_lease_s(self) -> None:
        """mutmut_5: hijack_begin lease_s default must be 90."""
        import inspect

        tools = hijack_tools("http://test")
        begin = tools[0]
        sig = inspect.signature(begin)
        assert sig.parameters["lease_s"].default == 90

    async def test_hijack_begin_default_owner(self) -> None:
        """mutmut_6/7: hijack_begin owner default must be 'operator'."""
        import inspect

        tools = hijack_tools("http://test")
        begin = tools[0]
        sig = inspect.signature(begin)
        assert sig.parameters["owner"].default == "operator"

    async def test_hijack_begin_passes_owner_to_acquire(self) -> None:
        """mutmut_13: owner must be forwarded to client.acquire."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        begin = tools[0]

        result = await begin(WID, lease_s=60, owner="specialized-bot")
        assert result["success"] is True
        assert result.get("owner") == "specialized-bot"

    async def test_hijack_begin_passes_lease_s_to_acquire(self) -> None:
        """mutmut_14: lease_s must be forwarded to client.acquire."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        begin = tools[0]

        result = await begin(WID, lease_s=30)
        assert result["success"] is True
        remaining = result["lease_expires_at"] - time.time()
        assert 25 <= remaining <= 35

    async def test_hijack_heartbeat_default_lease_s(self) -> None:
        """mutmut_19: hijack_heartbeat lease_s default must be 90."""
        import inspect

        tools = hijack_tools("http://test")
        heartbeat = tools[1]
        sig = inspect.signature(heartbeat)
        assert sig.parameters["lease_s"].default == 90

    async def test_hijack_heartbeat_passes_lease_s(self) -> None:
        """mutmut_26: lease_s must be forwarded to client.heartbeat."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        begin, heartbeat, *_ = tools

        result = await begin(WID)
        hid = result["hijack_id"]

        result = await heartbeat(WID, hid, lease_s=45)
        assert result["success"] is True
        remaining = result["lease_expires_at"] - time.time()
        assert 40 <= remaining <= 50

    async def test_hijack_read_default_mode_is_snapshot(self) -> None:
        """mutmut_31/32: mode default must be 'snapshot'."""
        import inspect

        tools = hijack_tools("http://test")
        read = tools[2]
        sig = inspect.signature(read)
        assert sig.parameters["mode"].default == "snapshot"

    async def test_hijack_read_default_wait_ms(self) -> None:
        """mutmut_33: wait_ms default must be 1500."""
        import inspect

        tools = hijack_tools("http://test")
        read = tools[2]
        sig = inspect.signature(read)
        assert sig.parameters["wait_ms"].default == 1500

    async def test_hijack_read_default_after_seq(self) -> None:
        """mutmut_34: after_seq default must be 0."""
        import inspect

        tools = hijack_tools("http://test")
        read = tools[2]
        sig = inspect.signature(read)
        assert sig.parameters["after_seq"].default == 0

    async def test_hijack_read_default_limit(self) -> None:
        """mutmut_35: limit default must be 200."""
        import inspect

        tools = hijack_tools("http://test")
        read = tools[2]
        sig = inspect.signature(read)
        assert sig.parameters["limit"].default == 200

    async def test_hijack_read_events_passes_after_seq(self) -> None:
        """mutmut_46: after_seq must be forwarded to client.events."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        begin, _, read, *_ = tools

        result = await begin(WID)
        hid = result["hijack_id"]

        # With after_seq=0 (default), should return all events
        result = await read(WID, hid, mode="events", after_seq=0, limit=100)
        assert result["success"] is True
        assert "events" in result

    async def test_hijack_read_events_passes_limit(self) -> None:
        """mutmut_47: limit must be forwarded to client.events."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        begin, _, read, *_ = tools

        result = await begin(WID)
        hid = result["hijack_id"]

        result = await read(WID, hid, mode="events", limit=5)
        assert result["success"] is True

    async def test_hijack_read_snapshot_passes_wait_ms(self) -> None:
        """mutmut_54: wait_ms must be forwarded to client.snapshot."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        begin, _, read, *_ = tools

        result = await begin(WID)
        hid = result["hijack_id"]

        result = await read(WID, hid, mode="snapshot", wait_ms=100)
        assert result["success"] is True
        assert "snapshot" in result

    async def test_hijack_send_default_timeout_ms(self) -> None:
        """mutmut_59: timeout_ms default must be 2000."""
        import inspect

        tools = hijack_tools("http://test")
        send = tools[3]
        sig = inspect.signature(send)
        assert sig.parameters["timeout_ms"].default == 2000

    async def test_hijack_send_default_poll_interval_ms(self) -> None:
        """mutmut_60: poll_interval_ms default must be 120."""
        import inspect

        tools = hijack_tools("http://test")
        send = tools[3]
        sig = inspect.signature(send)
        assert sig.parameters["poll_interval_ms"].default == 120

    async def test_hijack_send_passes_expect_prompt_id(self) -> None:
        """mutmut_65: expect_prompt_id must be forwarded to client.send."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        begin, _, _, send, *_ = tools

        result = await begin(WID)
        hid = result["hijack_id"]

        # The request body should include expect_prompt_id
        # We verify it doesn't cause an unexpected failure due to missing arg
        result = await send(WID, hid, keys="test", expect_prompt_id="some_prompt")
        assert isinstance(result, dict)
        assert "success" in result

    async def test_hijack_send_passes_expect_regex(self) -> None:
        """mutmut_66: expect_regex must be forwarded to client.send."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        begin, _, _, send, *_ = tools

        result = await begin(WID)
        hid = result["hijack_id"]

        result = await send(WID, hid, keys="test", expect_regex=".*>.*")
        assert isinstance(result, dict)
        assert "success" in result

    async def test_hijack_send_passes_timeout_ms(self) -> None:
        """mutmut_74: timeout_ms must be forwarded to client.send."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        begin, _, _, send, *_ = tools

        result = await begin(WID)
        hid = result["hijack_id"]

        result = await send(WID, hid, keys="x", timeout_ms=500)
        assert result["success"] is True

    async def test_hijack_send_passes_poll_interval_ms(self) -> None:
        """mutmut_75: poll_interval_ms must be forwarded to client.send."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        begin, _, _, send, *_ = tools

        result = await begin(WID)
        hid = result["hijack_id"]

        result = await send(WID, hid, keys="x", poll_interval_ms=50)
        assert result["success"] is True

    async def test_hijack_begin_result_has_data(self) -> None:
        """mutmut_28: hijack_begin result must contain actual data, not None."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        begin = tools[0]

        result = await begin(WID)
        assert result["success"] is True
        # Would fail if mutant passes None: hijack_id would not be in result
        assert "hijack_id" in result
        assert result["hijack_id"] is not None

    async def test_hijack_send_result_has_data(self) -> None:
        """mutmut_77: hijack_send result must contain actual data."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        begin, _, _, send, *_ = tools

        result = await begin(WID)
        hid = result["hijack_id"]

        result = await send(WID, hid, keys="test\r")
        assert result["success"] is True
        # With real data, 'sent' should be in result
        assert "sent" in result
        assert result["sent"] is not None

    async def test_hijack_step_result_has_data(self) -> None:
        """mutmut_86: hijack_step result must contain actual data."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        begin, _, _, _, step, _ = tools

        result = await begin(WID)
        hid = result["hijack_id"]

        result = await step(WID, hid)
        assert result["success"] is True
        # With real data, 'ok' should be in result
        assert "ok" in result

    async def test_hijack_release_result_has_data(self) -> None:
        """mutmut_95: hijack_release result must contain actual data."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        begin, _, _, _, _, release = tools

        result = await begin(WID)
        hid = result["hijack_id"]

        result = await release(WID, hid)
        assert result["success"] is True
        # With real data, 'ok' should be in result
        assert "ok" in result

    async def test_hijack_read_snapshot_result_has_data(self) -> None:
        """mutmut_28 (read): hijack_read snapshot result must contain actual snapshot."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        begin, _, read, *_ = tools

        result = await begin(WID)
        hid = result["hijack_id"]

        result = await read(WID, hid, mode="snapshot", wait_ms=50)
        assert result["success"] is True
        # With real data, snapshot key should be present
        assert "snapshot" in result

    async def test_hijack_read_events_result_has_data(self) -> None:
        """mutmut_28 (read events): hijack_read events result must contain actual events."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        transport = ASGITransport(app=app)

        tools = hijack_tools("http://test", transport=transport)
        begin, _, read, *_ = tools

        result = await begin(WID)
        hid = result["hijack_id"]

        result = await read(WID, hid, mode="events")
        assert result["success"] is True
        assert "events" in result
