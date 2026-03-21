#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for client — snapshot, events, session management, quick-connect."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport

from provide.terminal.client.hijack import HijackClient
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


def _client_for(app: FastAPI, **kwargs: Any) -> HijackClient:
    return HijackClient(
        "http://test",
        transport=ASGITransport(app=app),
        **kwargs,  # type: ignore[arg-type]
    )


def _server_app() -> FastAPI:
    from provide.terminal.server.app import create_server_app
    from provide.terminal.server.config import config_from_mapping

    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 0},
            "auth": {"mode": "dev"},
            "sessions": [
                {
                    "session_id": "s1",
                    "display_name": "Test",
                    "connector_type": "shell",
                    "auto_start": False,
                }
            ],
        }
    )
    return create_server_app(cfg)


def _server_client(app: FastAPI) -> HijackClient:
    return HijackClient(
        "http://test",
        transport=ASGITransport(app=app),
        headers={"X-Uterm-Principal": "tester", "X-Uterm-Role": "admin"},
    )


class TestSnapshotMutants:
    async def test_snapshot_default_wait_ms_is_1500(self) -> None:
        """mutmut_1: wait_ms default must be 1500."""
        import inspect

        sig = inspect.signature(HijackClient.snapshot)
        assert sig.parameters["wait_ms"].default == 1500

    async def test_snapshot_sends_wait_ms_param(self) -> None:
        """mutmut_4/7/14/15: wait_ms must be sent as 'wait_ms' query param."""
        captured_params: list[dict] = []
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, acq = await c.acquire(WID)
            hid = acq["hijack_id"]

            original_request = c._request.__func__

            async def _intercepting_request(self_c, method, path, *, json=None, params=None):
                if "snapshot" in path:
                    captured_params.append(params or {})
                return await original_request(self_c, method, path, json=json, params=params)

            import types

            c._request = types.MethodType(_intercepting_request, c)
            await c.snapshot(WID, hid, wait_ms=999)

        if captured_params:
            assert "wait_ms" in captured_params[0]
            assert captured_params[0]["wait_ms"] == 999

    async def test_snapshot_without_wait_ms_uses_default(self) -> None:
        """mutmut_4: params=None would drop wait_ms entirely."""
        captured_params: list[dict] = []
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, acq = await c.acquire(WID)
            hid = acq["hijack_id"]

            original_request = c._request.__func__

            async def _intercepting_request(self_c, method, path, *, json=None, params=None):
                if "snapshot" in path:
                    captured_params.append(params or {})
                return await original_request(self_c, method, path, json=json, params=params)

            import types

            c._request = types.MethodType(_intercepting_request, c)
            await c.snapshot(WID, hid)  # default wait_ms=1500

        if captured_params:
            assert "wait_ms" in captured_params[0]
            assert captured_params[0]["wait_ms"] == 1500


# ---------------------------------------------------------------------------
# HijackClient.events — mutmut_1/2: default values
# mutmut_17/18: wrong param keys
# ---------------------------------------------------------------------------


class TestEventsMutants:
    async def test_events_default_after_seq_is_0(self) -> None:
        """mutmut_1: after_seq default must be 0."""
        import inspect

        sig = inspect.signature(HijackClient.events)
        assert sig.parameters["after_seq"].default == 0

    async def test_events_default_limit_is_200(self) -> None:
        """mutmut_2: limit default must be 200."""
        import inspect

        sig = inspect.signature(HijackClient.events)
        assert sig.parameters["limit"].default == 200

    async def test_events_sends_correct_param_keys(self) -> None:
        """mutmut_17/18: params keys must be 'after_seq' and 'limit'."""
        captured_params: list[dict] = []
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, acq = await c.acquire(WID)
            hid = acq["hijack_id"]

            original_request = c._request.__func__

            async def _intercepting_request(self_c, method, path, *, json=None, params=None):
                if "events" in path:
                    captured_params.append(params or {})
                return await original_request(self_c, method, path, json=json, params=params)

            import types

            c._request = types.MethodType(_intercepting_request, c)
            await c.events(WID, hid, after_seq=5, limit=50)

        if captured_params:
            assert "after_seq" in captured_params[0]
            assert "limit" in captured_params[0]
            assert captured_params[0]["after_seq"] == 5
            assert captured_params[0]["limit"] == 50

    async def test_events_sends_correct_param_values(self) -> None:
        """mutmut_17/18: param keys must not have XX or uppercase variants."""
        captured_params: list[dict] = []
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, acq = await c.acquire(WID)
            hid = acq["hijack_id"]

            original_request = c._request.__func__

            async def _intercepting_request(self_c, method, path, *, json=None, params=None):
                if "events" in path:
                    captured_params.append(params or {})
                return await original_request(self_c, method, path, json=json, params=params)

            import types

            c._request = types.MethodType(_intercepting_request, c)
            await c.events(WID, hid)

        if captured_params:
            assert "XXlimitXX" not in captured_params[0]
            assert "LIMIT" not in captured_params[0]
            assert "after_seq" in captured_params[0]
            assert "limit" in captured_params[0]


# ---------------------------------------------------------------------------
# HijackClient.session_events — mutmut_1/4/7/10/11
# ---------------------------------------------------------------------------


class TestSessionEventsMutants:
    async def test_session_events_default_limit_is_100(self) -> None:
        """mutmut_1: session_events limit default must be 100."""
        import inspect

        sig = inspect.signature(HijackClient.session_events)
        assert sig.parameters["limit"].default == 100

    async def test_session_events_sends_limit_param(self) -> None:
        """mutmut_4/7/10/11: limit must be sent as 'limit' param."""
        app = _server_app()
        async with _server_client(app) as c:
            ok, data = await c.session_events("s1", limit=25)
        assert ok is True

    async def test_session_events_limit_correct_key(self) -> None:
        """mutmut_10/11: params key must be 'limit' not 'XXlimitXX' or 'LIMIT'."""
        captured_params: list[dict] = []
        app = _server_app()

        async with _server_client(app) as c:
            original_request = c._request.__func__

            async def _intercepting_request(self_c, method, path, *, json=None, params=None):
                if "events" in path:
                    captured_params.append(params or {})
                return await original_request(self_c, method, path, json=json, params=params)

            import types

            c._request = types.MethodType(_intercepting_request, c)
            await c.session_events("s1", limit=77)

        if captured_params:
            assert "limit" in captured_params[0]
            assert captured_params[0]["limit"] == 77
            assert "XXlimitXX" not in captured_params[0]
            assert "LIMIT" not in captured_params[0]


# ---------------------------------------------------------------------------
# HijackClient.quick_connect — mutmut_2/3: wrong connector_type key
# mutmut_4: display_name is not None → is None
# mutmut_5/6/7: display_name body value/key mutations
# ---------------------------------------------------------------------------


class TestQuickConnectMutants:
    async def test_quick_connect_sends_connector_type(self) -> None:
        """mutmut_2/3: body must use key 'connector_type'."""
        app = _server_app()
        async with _server_client(app) as c:
            ok, data = await c.quick_connect("shell")
        assert ok is True
        assert "session_id" in data

    async def test_quick_connect_body_connector_type_key(self) -> None:
        """mutmut_2/3: key in body must be 'connector_type' not 'XXconnector_typeXX'."""
        captured_json: list[dict] = []
        app = _server_app()

        async with _server_client(app) as c:
            original_request = c._request.__func__

            async def _intercepting_request(self_c, method, path, *, json=None, params=None):
                if "connect" in path and json:
                    captured_json.append(dict(json))
                return await original_request(self_c, method, path, json=json, params=params)

            import types

            c._request = types.MethodType(_intercepting_request, c)
            await c.quick_connect("shell", display_name="Test")

        if captured_json:
            assert "connector_type" in captured_json[0]
            assert captured_json[0]["connector_type"] == "shell"
            assert "XXconnector_typeXX" not in captured_json[0]
            assert "CONNECTOR_TYPE" not in captured_json[0]

    async def test_quick_connect_without_display_name_excludes_key(self) -> None:
        """mutmut_4: when display_name is None, key must NOT be in body."""
        captured_json: list[dict] = []
        app = _server_app()

        async with _server_client(app) as c:
            original_request = c._request.__func__

            async def _intercepting_request(self_c, method, path, *, json=None, params=None):
                if "connect" in path and json:
                    captured_json.append(dict(json))
                return await original_request(self_c, method, path, json=json, params=params)

            import types

            c._request = types.MethodType(_intercepting_request, c)
            await c.quick_connect("shell")  # no display_name

        if captured_json:
            assert "display_name" not in captured_json[0]

    async def test_quick_connect_with_display_name_includes_correct_value(self) -> None:
        """mutmut_4/5/6/7: display_name value must not be None and key must be 'display_name'."""
        captured_json: list[dict] = []
        app = _server_app()

        async with _server_client(app) as c:
            original_request = c._request.__func__

            async def _intercepting_request(self_c, method, path, *, json=None, params=None):
                if "connect" in path and json:
                    captured_json.append(dict(json))
                return await original_request(self_c, method, path, json=json, params=params)

            import types

            c._request = types.MethodType(_intercepting_request, c)
            await c.quick_connect("shell", display_name="My Session")

        if captured_json:
            assert "display_name" in captured_json[0]
            assert captured_json[0]["display_name"] == "My Session"
            assert captured_json[0]["display_name"] is not None
            assert "XXdisplay_nameXX" not in captured_json[0]
            assert "DISPLAY_NAME" not in captured_json[0]


# ---------------------------------------------------------------------------
# mcp_tools._ok — mutmut_28/77/86/95: _ok(ok, None) instead of _ok(ok, data)
# ---------------------------------------------------------------------------
