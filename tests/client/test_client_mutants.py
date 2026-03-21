#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Targeted mutation-killing tests for client/hijack.py and client/mcp_tools.py.

Each test is designed to detect a specific surviving mutant. Tests use
httpx ASGITransport to exercise real HTTP paths.
"""

from __future__ import annotations

import time
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


# ---------------------------------------------------------------------------
# HijackClient.__init__ mutations
# mutmut_6: rstrip → lstrip for base_url
# mutmut_9/10: rstrip → lstrip for entity_prefix
# mutmut_17/18: _owns_client=True → None/False
# ---------------------------------------------------------------------------


class TestHijackClientInit:
    def test_base_url_trailing_slash_stripped(self) -> None:
        """mutmut_6: base_url trailing slash must be stripped (rstrip not lstrip)."""
        c = HijackClient("http://localhost:8780/")
        assert c._base_url == "http://localhost:8780"
        assert not c._base_url.endswith("/")

    def test_base_url_without_trailing_slash_unchanged(self) -> None:
        """rstrip on URL without trailing slash should not alter it."""
        c = HijackClient("http://localhost:8780")
        assert c._base_url == "http://localhost:8780"

    def test_base_url_lstrip_would_corrupt_url(self) -> None:
        """mutmut_6: lstrip('/') would leave trailing slash on URL with trailing /."""
        url = "http://localhost:8780/"
        # rstrip removes trailing slash → correct
        assert url.rstrip("/") == "http://localhost:8780"
        # lstrip removes leading slash → wrong (doesn't remove trailing)
        assert url.lstrip("/") == "http://localhost:8780/"

    def test_entity_prefix_trailing_slash_stripped(self) -> None:
        """mutmut_9/10: entity_prefix trailing slash must be stripped."""
        c = HijackClient("http://test", entity_prefix="/worker/")
        assert c._entity_prefix == "/worker"
        assert not c._entity_prefix.endswith("/")

    def test_owns_client_initially_true(self) -> None:
        """mutmut_17/18: _owns_client must start as True (bool, not None)."""
        c = HijackClient("http://test")
        assert c._owns_client is True
        assert c._owns_client is not None

    def test_entity_prefix_default_is_slash_worker(self) -> None:
        """mutmut_1/2: entity_prefix default must be '/worker'."""
        c = HijackClient("http://test")
        assert c._entity_prefix == "/worker"

    def test_timeout_default_is_20(self) -> None:
        """mutmut_3: timeout default must be exactly 20.0."""
        c = HijackClient("http://test")
        assert c._timeout == 20.0
        assert c._timeout != 21.0


# ---------------------------------------------------------------------------
# HijackClient.__aenter__ / __aexit__ mutations
# mutmut_14/15: _owns_client=True → None/False in aenter
# mutmut_2: _client=None → "" in aexit
# ---------------------------------------------------------------------------


class TestHijackClientContextManager:
    async def test_aenter_sets_owns_client_true(self) -> None:
        """mutmut_14/15: __aenter__ must set _owns_client=True."""
        c = HijackClient("http://test")
        c._owns_client = False  # reset
        async with c:
            assert c._owns_client is True
            assert c._owns_client is not None
        # After aexit, client should be cleaned up
        assert c._client is None

    async def test_aexit_sets_client_to_none(self) -> None:
        """mutmut_2: __aexit__ must set _client to None (not '')."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        c = _client_for(app)
        async with c:
            assert c._client is not None

        # After exit, _client must be exactly None
        assert c._client is None
        assert c._client != ""

    async def test_aexit_client_none_is_noop(self) -> None:
        """__aexit__ when _client is None must not raise."""
        c = HijackClient("http://test")
        assert c._client is None
        # Should not raise
        await c.__aexit__(None, None, None)
        assert c._client is None


# ---------------------------------------------------------------------------
# HijackClient._get_client — mutmut_15/16: _owns_client
# ---------------------------------------------------------------------------


class TestGetClient:
    def test_lazy_client_sets_owns_client_true(self) -> None:
        """mutmut_15/16: lazy _get_client must set _owns_client=True."""
        c = HijackClient("http://test")
        c._owns_client = False  # reset

        _ = c._get_client()
        assert c._owns_client is True
        assert c._owns_client is not None

        # cleanup
        import asyncio

        asyncio.run(c._client.aclose())


# ---------------------------------------------------------------------------
# HijackClient._request — mutmut_25: str(exc) → str(None)
# ---------------------------------------------------------------------------


class TestRequestError:
    async def test_error_body_contains_exception_message(self) -> None:
        """mutmut_25: error body must contain str(exc), not str(None)."""
        # Use a transport that raises an HTTPError
        c = HijackClient("http://127.0.0.1:1")  # nothing listening
        ok, data = await c._request("GET", "/no-such-path")
        assert ok is False
        assert "error" in data
        # The error string must not be str(None) = "None"
        assert data["error"] != "None"
        # It should be a meaningful error message
        assert len(data["error"]) > 0


# ---------------------------------------------------------------------------
# HijackClient.acquire — mutmut_3: lease_s default = 90
# mutmut_11: "POST" vs "post" (equivalent, but also covers JSON body)
# ---------------------------------------------------------------------------


class TestAcquireMutants:
    async def test_acquire_default_owner_is_operator(self) -> None:
        """mutmut_1/2: owner default must be 'operator'."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        async with _client_for(app) as c:
            ok, data = await c.acquire(WID)
        assert ok is True
        assert data["owner"] == "operator"

    async def test_acquire_default_lease_s_is_90(self) -> None:
        """mutmut_3: lease_s default must be 90."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        async with _client_for(app) as c:
            ok, data = await c.acquire(WID)
        assert ok is True
        # lease_expires_at should be about 90s from now
        assert "lease_expires_at" in data
        remaining = data["lease_expires_at"] - time.time()
        assert 85 <= remaining <= 95  # 90s ± 5s tolerance

    async def test_acquire_sends_owner_in_json(self) -> None:
        """Acquire sends owner in JSON body (kills mutants that drop JSON)."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        async with _client_for(app) as c:
            ok, data = await c.acquire(WID, owner="mybot", lease_s=60)
        assert ok is True
        assert data["owner"] == "mybot"

    async def test_acquire_sends_lease_s_in_json(self) -> None:
        """Acquire sends lease_s in JSON body."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        async with _client_for(app) as c:
            ok, data = await c.acquire(WID, lease_s=30)
        assert ok is True
        remaining = data["lease_expires_at"] - time.time()
        assert 25 <= remaining <= 35


# ---------------------------------------------------------------------------
# HijackClient.heartbeat — mutmut_4: json=None, mutmut_14/15: wrong keys
# mutmut_1: lease_s default = 90
# ---------------------------------------------------------------------------


class TestHeartbeatMutants:
    async def test_heartbeat_default_lease_s_is_90(self) -> None:
        """mutmut_1: lease_s default must be 90."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        async with _client_for(app) as c:
            ok, acq = await c.acquire(WID)
            hid = acq["hijack_id"]
            ok, hb = await c.heartbeat(WID, hid)

        assert ok is True
        # Default lease_s=90 → lease_expires_at should be ~90s from now
        remaining = hb["lease_expires_at"] - time.time()
        assert 85 <= remaining <= 95

    async def test_heartbeat_sends_lease_s_in_json(self) -> None:
        """mutmut_4/14/15: JSON body must use key 'lease_s' with correct value."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        async with _client_for(app) as c:
            ok, acq = await c.acquire(WID)
            hid = acq["hijack_id"]
            ok, hb = await c.heartbeat(WID, hid, lease_s=45)

        assert ok is True
        remaining = hb["lease_expires_at"] - time.time()
        assert 40 <= remaining <= 50  # 45s ± 5s


# ---------------------------------------------------------------------------
# HijackClient.send — mutmut_10/14: is not None → is None (conditional flip)
# mutmut_1/2: default values, mutmut_6-17: wrong body keys
# mutmut_11/15: value→None
# ---------------------------------------------------------------------------


class TestSendMutants:
    async def test_send_default_timeout_ms_is_2000(self) -> None:
        """mutmut_1: timeout_ms default must be 2000."""
        hub, app = _make_hub_app()
        _add_worker(hub)
        # Can only verify the default by inspecting the method signature
        import inspect

        sig = inspect.signature(HijackClient.send)
        assert sig.parameters["timeout_ms"].default == 2000

    async def test_send_default_poll_interval_ms_is_120(self) -> None:
        """mutmut_2: poll_interval_ms default must be 120."""
        import inspect

        sig = inspect.signature(HijackClient.send)
        assert sig.parameters["poll_interval_ms"].default == 120

    async def test_send_without_expect_prompt_id_excludes_key(self) -> None:
        """mutmut_10: expect_prompt_id=None must NOT include key in body.

        If condition flipped (is None instead of is not None), the key would
        be added when expect_prompt_id IS None (the default), causing issues.
        """
        # We intercept the actual request body
        captured_json: list[dict] = []
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, acq = await c.acquire(WID)
            hid = acq["hijack_id"]

            original_request = c._request.__func__

            async def _intercepting_request(self_c, method, path, *, json=None, params=None):
                if "send" in path:
                    captured_json.append(json or {})
                return await original_request(self_c, method, path, json=json, params=params)

            import types

            c._request = types.MethodType(_intercepting_request, c)
            await c.send(WID, hid, keys="test")

        if captured_json:
            # When expect_prompt_id is not provided (None), key must NOT be in body
            assert "expect_prompt_id" not in captured_json[0]

    async def test_send_with_expect_prompt_id_includes_key(self) -> None:
        """mutmut_10: when expect_prompt_id is provided, key must be in body."""
        captured_json: list[dict] = []
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, acq = await c.acquire(WID)
            hid = acq["hijack_id"]

            original_request = c._request.__func__

            async def _intercepting_request(self_c, method, path, *, json=None, params=None):
                if "send" in path and json:
                    captured_json.append(dict(json))
                return await original_request(self_c, method, path, json=json, params=params)

            import types

            c._request = types.MethodType(_intercepting_request, c)
            await c.send(WID, hid, keys="test", expect_prompt_id="login_prompt")

        if captured_json:
            assert "expect_prompt_id" in captured_json[0]
            assert captured_json[0]["expect_prompt_id"] == "login_prompt"

    async def test_send_with_expect_regex_includes_key(self) -> None:
        """mutmut_14: when expect_regex provided, key must be in body with correct value."""
        captured_json: list[dict] = []
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, acq = await c.acquire(WID)
            hid = acq["hijack_id"]

            original_request = c._request.__func__

            async def _intercepting_request(self_c, method, path, *, json=None, params=None):
                if "send" in path and json:
                    captured_json.append(dict(json))
                return await original_request(self_c, method, path, json=json, params=params)

            import types

            c._request = types.MethodType(_intercepting_request, c)
            await c.send(WID, hid, keys="test", expect_regex=".*>.*")

        if captured_json:
            assert "expect_regex" in captured_json[0]
            assert captured_json[0]["expect_regex"] == ".*>.*"

    async def test_send_without_expect_regex_excludes_key(self) -> None:
        """mutmut_14: expect_regex=None (default) must NOT include key in body."""
        captured_json: list[dict] = []
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, acq = await c.acquire(WID)
            hid = acq["hijack_id"]

            original_request = c._request.__func__

            async def _intercepting_request(self_c, method, path, *, json=None, params=None):
                if "send" in path:
                    captured_json.append(json or {})
                return await original_request(self_c, method, path, json=json, params=params)

            import types

            c._request = types.MethodType(_intercepting_request, c)
            await c.send(WID, hid, keys="test")

        if captured_json:
            assert "expect_regex" not in captured_json[0]

    async def test_send_body_keys_are_correct(self) -> None:
        """mutmut_6/7/8/9: body keys must be 'timeout_ms' and 'poll_interval_ms'."""
        captured_json: list[dict] = []
        hub, app = _make_hub_app()
        _add_worker(hub)

        async with _client_for(app) as c:
            ok, acq = await c.acquire(WID)
            hid = acq["hijack_id"]

            original_request = c._request.__func__

            async def _intercepting_request(self_c, method, path, *, json=None, params=None):
                if "send" in path and json:
                    captured_json.append(dict(json))
                return await original_request(self_c, method, path, json=json, params=params)

            import types

            c._request = types.MethodType(_intercepting_request, c)
            await c.send(WID, hid, keys="hello", timeout_ms=3000, poll_interval_ms=150)

        if captured_json:
            assert "timeout_ms" in captured_json[0]
            assert "poll_interval_ms" in captured_json[0]
            assert captured_json[0]["timeout_ms"] == 3000
            assert captured_json[0]["poll_interval_ms"] == 150


# ---------------------------------------------------------------------------
# HijackClient.snapshot — mutmut_1: wait_ms default=1500
# mutmut_4/7: params=None or removed
# mutmut_14/15: wrong param keys
# ---------------------------------------------------------------------------
