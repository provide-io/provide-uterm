"""Conformance slice — align CF and FastAPI behavior on session surfaces."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from provide.uterm.cloudflare.api.http_routes import route_http
from provide.uterm.cloudflare.bridge.hijack import HijackCoordinator
from provide.uterm.cloudflare.entry import Default
from provide.uterm.cloudflare.state.store import SqliteStateStore


def _make_default(env_attrs: dict[str, object] | None = None) -> Default:
    attrs: dict[str, object] = {"AUTH_MODE": "dev"}
    if env_attrs:
        attrs.update(env_attrs)
    return Default(SimpleNamespace(**attrs))


def _req(path: str, *, method: str = "GET") -> SimpleNamespace:
    return SimpleNamespace(url=f"https://example.invalid{path}", method=method, headers={})


def _html_text(resp: object) -> str:
    body = getattr(resp, "body", "")
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return str(body)


class _CfRuntime:
    def __init__(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.store = SqliteStateStore(conn.execute)
        self.store.migrate()
        self.worker_id = "delete-sess"
        self.meta = {
            "display_name": self.worker_id,
            "connector_type": "shell",
            "created_at": 0.0,
            "tags": [],
            "visibility": "public",
            "owner": None,
        }
        self.worker_ws = object()
        self.browser_sockets: dict[str, object] = {}
        self.raw_sockets: dict[str, object] = {}
        self.hijack = HijackCoordinator()
        self._deleted_at = None
        self._role = "admin"
        self._subject = None
        self.last_snapshot = None
        self.last_analysis = None
        self.lifecycle_state = "stopped"
        self.input_mode = "hijack"

    async def request_json(self, request: object) -> dict:
        return json.loads(getattr(request, "_body", "{}"))

    async def browser_role_for_request(self, request: object) -> str:
        return self._role

    async def browser_subject_for_request(self, request: object) -> str | None:
        return self._subject

    def persist_lease(self, session: object) -> None:
        pass

    def clear_lease(self) -> None:
        pass

    async def push_worker_control(self, action: str, *, owner: str, lease_s: int) -> bool:
        return self.worker_ws is not None

    async def broadcast_hijack_state(self) -> None:
        pass

    async def push_worker_input(self, data: str) -> bool:
        return self.worker_ws is not None

    async def send_ws(self, ws: object, frame: dict) -> None:
        pass


def _route_req(path: str, *, method: str = "GET") -> SimpleNamespace:
    return SimpleNamespace(url=f"https://example.invalid{path}", method=method, headers={}, _body="{}")


async def test_share_token_is_not_exposed_in_html_bootstrap() -> None:
    kv = SimpleNamespace(
        get=AsyncMock(
            return_value=json.dumps(
                {
                    "share_token": "share-token-123",
                    "control_token": "control-token-123",
                }
            )
        )
    )
    d = _make_default({"SESSION_REGISTRY": kv})

    resp = await d.fetch(_req("/app/session/share-sess?token=share-token-123"))
    html = _html_text(resp)

    assert resp.status == 200
    assert "share-token-123" not in html
    assert '"share_token"' not in html


async def test_deleted_session_id_is_not_readable_after_delete() -> None:
    runtime = _CfRuntime()
    delete_resp = await route_http(runtime, _route_req("/api/sessions/delete-sess", method="DELETE"))
    after_resp = await route_http(runtime, _route_req("/api/sessions/delete-sess"))

    assert delete_resp.status == 200
    assert after_resp.status == 404
