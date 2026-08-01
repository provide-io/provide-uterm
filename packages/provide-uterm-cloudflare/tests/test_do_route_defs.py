"""RouteDef dispatch coverage for Durable Object HTTP routes."""

from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from provide.uterm.api_routes import API_ROUTES, RouteScope
from provide.uterm.cloudflare.api.http_routes import route_http
from provide.uterm.cloudflare.bridge.hijack import HijackCoordinator
from provide.uterm.cloudflare.state.store import SqliteStateStore


class _Request:
    def __init__(self, path: str, method: str = "GET", body: dict[str, object] | None = None) -> None:
        self.url = f"https://example.invalid{path}"
        self.method = method
        self.headers: dict[str, str] = {}
        self._body = json.dumps(body or {})


class _Runtime:
    def __init__(self) -> None:
        self.worker_id = "session-1"
        self.meta = {"visibility": "public", "owner": "owner"}
        self.worker_ws: object | None = object()
        self.hijack = HijackCoordinator()
        self.lifecycle_state = "running"
        self.input_mode = "open"
        self.last_snapshot = None
        self.last_analysis = None
        self.browser_hijack_owner: dict[str, str] = {}
        self._deleted_at = None
        self.actions: list[str] = []
        self.store = SimpleNamespace(
            list_events_since=lambda *_args: [{"seq": 2, "type": "snapshot"}],
            current_event_seq=lambda *_args: 2,
            min_event_seq=lambda *_args: 1,
            load_session=lambda *_args: None,
            save_input_mode=lambda *_args: None,
        )

    def input_delivery_guard(self):
        return nullcontext()

    async def request_json(self, request: object) -> dict[str, object]:
        return json.loads(request._body)

    async def browser_role_for_request(self, _request: object) -> str:
        return "admin"

    async def browser_subject_for_request(self, _request: object) -> str:
        return "owner"

    async def push_worker_control(self, action: str, *, owner: str, lease_s: int) -> bool:
        self.actions.append(action)
        return self.worker_ws is not None

    async def broadcast_hijack_state(self) -> None:
        return

    async def send_ws(self, _ws: object, _frame: dict[str, object]) -> None:
        return


async def test_do_route_defs_dispatch_connect_disconnect_and_events_watch() -> None:
    runtime = _Runtime()

    connect = await route_http(runtime, _Request("/api/sessions/session-1/connect", "POST"))
    disconnect = await route_http(runtime, _Request("/api/sessions/session-1/disconnect", "POST"))
    watch = await route_http(runtime, _Request("/api/sessions/session-1/events/watch"))

    assert connect.status == 200
    assert disconnect.status == 200
    assert json.loads(watch.body) == {
        "events": [{"seq": 2, "type": "snapshot"}],
        "dropped_count": 0,
        "timed_out": False,
    }


async def test_do_dispatches_every_session_route_def_to_its_declared_capability() -> None:
    from provide.uterm.cloudflare.api.http_routes import _session

    session_routes = tuple(route for route in API_ROUTES if route.scope is RouteScope.SESSION)
    handlers = {
        route.capability: AsyncMock(return_value=SimpleNamespace(status=200, body="ok")) for route in session_routes
    }
    global_handlers = {
        route.capability: AsyncMock(return_value=SimpleNamespace(status=500, body="global"))
        for route in API_ROUTES
        if route.scope is RouteScope.GLOBAL
    }
    runtime = _Runtime()

    with (
        patch.dict(_session.SESSION_CAPABILITIES, handlers, clear=True),
        patch.dict("provide.uterm.cloudflare.entry.route_defs.GLOBAL_CAPABILITIES", global_handlers, clear=True),
    ):
        for route in session_routes:
            path = route.template.replace("{session_id}", runtime.worker_id).replace("{webhook_id}", "webhook-1")
            request = _Request(path, route.method)

            response = await route_http(runtime, request)

            assert response.status == 200
            handlers[route.capability].assert_awaited_once_with(
                runtime,
                request,
                path,
                request.url,
                route,
                {
                    "session_id": runtime.worker_id,
                    **({"webhook_id": "webhook-1"} if "{webhook_id}" in route.template else {}),
                },
            )

    assert all(handler.await_count == 0 for handler in global_handlers.values())


async def test_connect_does_not_claim_an_unstarted_ushell_is_connected() -> None:
    runtime = _Runtime()
    runtime.worker_ws = None
    runtime._ushell = object()

    response = await route_http(runtime, _Request("/api/sessions/session-1/connect", "POST"))

    assert response.status == 409
    assert json.loads(response.body) == {"error": "no_worker"}


async def test_started_ushell_is_connected_and_connect_is_idempotent() -> None:
    runtime = _Runtime()
    runtime.worker_ws = None
    runtime._ushell = SimpleNamespace(stop=AsyncMock())
    runtime._ushell_started = True

    status = await route_http(runtime, _Request("/api/sessions/session-1"))
    connect = await route_http(runtime, _Request("/api/sessions/session-1/connect", "POST"))

    assert json.loads(status.body)["connected"] is True
    assert connect.status == 200
    assert json.loads(connect.body)["connected"] is True


async def test_disconnect_stops_started_ushell_and_reflects_stopped_state() -> None:
    runtime = _Runtime()
    runtime.worker_ws = None
    runtime._ushell = SimpleNamespace(stop=AsyncMock())
    runtime._ushell_started = True

    response = await route_http(runtime, _Request("/api/sessions/session-1/disconnect", "POST"))

    runtime._ushell.stop.assert_awaited_once_with()
    assert runtime._ushell_started is False
    assert runtime.lifecycle_state == "stopped"
    assert json.loads(response.body)["connected"] is False


async def test_browser_start_transitions_native_ushell_to_running() -> None:
    from provide.uterm.cloudflare.do.ushell import on_browser_connected

    runtime = _Runtime()
    runtime.worker_ws = None
    runtime._ushell = SimpleNamespace(start=AsyncMock(), poll_messages=AsyncMock(return_value=[]))
    runtime._ushell_started = False
    runtime.lifecycle_state = "stopped"
    runtime.broadcast_worker_frame = AsyncMock()
    runtime.broadcast_to_browsers = AsyncMock()
    runtime.env = SimpleNamespace(SESSION_REGISTRY=_Kv())

    await on_browser_connected(runtime)

    runtime._ushell.start.assert_awaited_once_with()
    assert runtime._ushell_started is True
    assert runtime.lifecycle_state == "running"
    assert json.loads(runtime.env.SESSION_REGISTRY.values["session:session-1"])["lifecycle_state"] == "running"


async def test_native_ushell_disconnect_remains_fleet_listed_offline_after_alarm() -> None:
    from provide.uterm.cloudflare.do.session_runtime.io import _SessionRuntimeIoMixin
    from provide.uterm.cloudflare.do.ushell import on_browser_connected

    runtime = _persistent_runtime()
    runtime.worker_ws = None
    runtime._ushell = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), poll_messages=AsyncMock(return_value=[]))
    runtime._ushell_started = False
    runtime.broadcast_worker_frame = AsyncMock()
    runtime.broadcast_to_browsers = AsyncMock()

    await on_browser_connected(runtime)
    assert json.loads(runtime.env.SESSION_REGISTRY.values["session:session-1"])["connected"] is True

    await route_http(runtime, _Request("/api/sessions/session-1/disconnect", "POST"))
    await _SessionRuntimeIoMixin.alarm(runtime)

    fleet_row = json.loads(runtime.env.SESSION_REGISTRY.values["session:session-1"])
    assert fleet_row["connected"] is False
    assert fleet_row["lifecycle_state"] == "stopped"
    assert fleet_row["owner"] == "owner"


async def test_deleted_runtime_alarm_does_not_recreate_fleet_kv_or_reschedule() -> None:
    from provide.uterm.cloudflare.do.session_runtime.io import _SessionRuntimeIoMixin

    runtime = _persistent_runtime()
    set_alarm = Mock()
    runtime.ctx = SimpleNamespace(storage=SimpleNamespace(setAlarm=set_alarm))
    runtime.browser_sockets = {"browser": object()}
    runtime.raw_sockets = {"raw": object()}
    runtime._ushell = SimpleNamespace(stop=AsyncMock())
    runtime._ushell_started = True

    deleted = await route_http(runtime, _Request("/api/sessions/session-1", "DELETE"))
    assert deleted.status == 200
    runtime._ushell.stop.assert_awaited_once_with()
    assert runtime.worker_ws is None
    assert runtime.browser_sockets == {}
    assert runtime.raw_sockets == {}
    assert runtime._ushell_started is False
    # The Worker boundary removes the fleet record after this successful DO
    # response; alarm must not recreate it from stale in-memory socket state.
    runtime.env.SESSION_REGISTRY.values.pop("session:session-1")

    await _SessionRuntimeIoMixin.alarm(runtime)

    assert "session:session-1" not in runtime.env.SESSION_REGISTRY.values
    set_alarm.assert_not_called()


class _Kv:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def put(self, key: str, value: str) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _persistent_runtime() -> _Runtime:
    runtime = _Runtime()
    store = SqliteStateStore(sqlite3.connect(":memory:").execute)
    store.migrate()
    runtime.store = store
    kv = _Kv()
    kv.values["session:session-1"] = json.dumps(
        {
            "session_id": "session-1",
            "connected": True,
            "lifecycle_state": "running",
            "owner": "owner",
        }
    )
    runtime.env = SimpleNamespace(SESSION_REGISTRY=kv)
    return runtime


async def test_patch_persists_valid_metadata_and_updates_fleet_kv() -> None:
    runtime = _persistent_runtime()

    response = await route_http(
        runtime,
        _Request(
            "/api/sessions/session-1",
            "PATCH",
            {"display_name": "Prod shell", "tags": ["prod"], "visibility": "operator"},
        ),
    )

    assert response.status == 200
    restored = runtime.store.load_session_meta("session-1")
    assert restored is not None
    assert restored["display_name"] == "Prod shell"
    assert restored["tags"] == ["prod"]
    assert restored["visibility"] == "operator"
    assert restored["owner"] == "owner"
    fleet_row = json.loads(runtime.env.SESSION_REGISTRY.values["session:session-1"])
    assert fleet_row["display_name"] == "Prod shell"
    assert fleet_row["tags"] == ["prod"]
    assert fleet_row["visibility"] == "operator"


async def test_patch_rejects_unsupported_or_invalid_metadata_without_persisting() -> None:
    runtime = _persistent_runtime()

    invalid_visibility = await route_http(
        runtime,
        _Request("/api/sessions/session-1", "PATCH", {"visibility": "secret"}),
    )
    unsupported_field = await route_http(
        runtime,
        _Request("/api/sessions/session-1", "PATCH", {"owner": "attacker"}),
    )

    assert invalid_visibility.status == 422
    assert unsupported_field.status == 422
    assert runtime.store.load_session_meta("session-1") is None
    assert runtime.store.load_session_meta("session-1") is None
    assert json.loads(runtime.env.SESSION_REGISTRY.values["session:session-1"])["owner"] == "owner"


async def test_do_route_defs_preserve_registry_404_405_and_422_errors() -> None:
    runtime = _Runtime()

    unknown = await route_http(runtime, _Request("/api/sessions/session-1/nope"))
    wrong_method = await route_http(runtime, _Request("/api/sessions/session-1/events/watch", "POST"))
    invalid_parameter = await route_http(runtime, _Request("/api/sessions/not%20valid/events/watch"))

    assert unknown.status == 404
    assert json.loads(unknown.body) == {"error": "not_found", "path": "/api/sessions/session-1/nope"}
    assert wrong_method.status == 405
    assert wrong_method.headers["Allow"] == "GET"
    assert invalid_parameter.status == 422
    assert json.loads(invalid_parameter.body) == {"error": "invalid route parameter"}


async def test_do_never_executes_global_route_defs() -> None:
    response = await route_http(_Runtime(), _Request("/api/connect", "POST"))

    assert response.status == 404


def test_do_route_def_capabilities_cover_only_session_routes() -> None:
    from provide.uterm.cloudflare.api.http_routes import _session

    _session._validate_session_capabilities()

    with patch.dict(_session.SESSION_CAPABILITIES, {}, clear=True):
        with pytest.raises(ValueError, match="missing route capabilities"):
            _session._validate_session_capabilities()


def test_do_route_def_capability_validation_rejects_global_handler() -> None:
    from provide.uterm.cloudflare.api.http_routes import _session

    with patch.dict(_session.SESSION_CAPABILITIES, {"sessions.list": AsyncMock()}):
        with pytest.raises(ValueError, match="global RouteDef capability registered in Durable Object"):
            _session._validate_session_capabilities()
