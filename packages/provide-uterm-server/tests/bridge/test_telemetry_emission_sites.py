#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regression tests for telemetry emission at rate-limit and auth-denial sites.

These tests assert that:
- ``rate_limit.triggered`` is emitted with the correct ``limit_type`` and
  ``worker_id`` at each of the three rate-limit rejection paths in rest.py.
- ``auth.denied`` is emitted with the correct ``status``/``reason`` at each
  of the four denial branches in hub_authz.py.
- Each test **fails** if the emit line is removed (they assert the call, not
  just absence of an exception).

Coverage targets (rest.py):
  - ~line 122 (rest_acquire rate-limit + emit)
  - ~line 320 (rest_send rate-limit + emit)
  - ~line 400 (rest_step rate-limit + emit)

Coverage targets (hub_authz.py):
  - admin_required denial branch
  - unknown_session denial branch
  - read_denied denial branch
  - mutate_denied denial branch
  - _emit_denied no-op when uterm_hub is absent from app.state
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.server.bridge.hub import TermHub

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(**hub_kwargs: Any) -> tuple[TermHub, FastAPI, TestClient]:
    hub = TermHub(**hub_kwargs)
    app = FastAPI()
    app.include_router(hub.create_router())
    client = TestClient(app, raise_server_exceptions=True)
    return hub, app, client


# ---------------------------------------------------------------------------
# Rate-limit emission — rest_acquire
# ---------------------------------------------------------------------------


class TestRateLimitAcquireEmitsTelemetry:
    async def test_rate_limit_acquire_emits_event(self) -> None:
        """rate_limit.triggered with limit_type=rest_acquire is emitted on 429 acquire."""
        mock_sink = AsyncMock()
        hub, _app, client = _make_app(telemetry_sink=mock_sink)

        with patch.object(hub, "allow_rest_acquire_for", return_value=False):
            resp = client.post("/worker/wkr-1/hijack/acquire", json={})

        assert resp.status_code == 429
        assert resp.json()["error"] == "rate_limited"

        emitted = [c[0][0] for c in mock_sink.emit.call_args_list]
        rate_limited = [e for e in emitted if e.event_type == "rate_limit.triggered"]
        assert rate_limited, "rate_limit.triggered must be emitted on acquire rate-limit rejection"
        evt = rate_limited[0]
        assert evt.worker_id == "wkr-1"
        assert evt.metadata.get("limit_type") == "rest_acquire"
        assert evt.metadata.get("client_id") is not None


# ---------------------------------------------------------------------------
# Rate-limit emission — rest_send
# ---------------------------------------------------------------------------


class TestRateLimitSendEmitsTelemetry:
    async def test_rate_limit_send_emits_event(self) -> None:
        """rate_limit.triggered with limit_type=rest_send is emitted on 429 send."""
        mock_sink = AsyncMock()
        hub, _app, client = _make_app(telemetry_sink=mock_sink)
        hid = "abcdef12-0000-0000-0000-000000000000"

        with patch.object(hub, "allow_rest_send_for", return_value=False):
            resp = client.post(f"/worker/wkr-2/hijack/{hid}/send", json={"keys": "x"})

        assert resp.status_code == 429
        assert resp.json()["error"] == "rate_limited"

        emitted = [c[0][0] for c in mock_sink.emit.call_args_list]
        rate_limited = [e for e in emitted if e.event_type == "rate_limit.triggered"]
        assert rate_limited, "rate_limit.triggered must be emitted on send rate-limit rejection"
        evt = rate_limited[0]
        assert evt.worker_id == "wkr-2"
        assert evt.metadata.get("limit_type") == "rest_send"
        assert evt.metadata.get("client_id") is not None


# ---------------------------------------------------------------------------
# Rate-limit emission — rest_step
# ---------------------------------------------------------------------------


class TestRateLimitStepEmitsTelemetry:
    async def test_rate_limit_step_emits_event(self) -> None:
        """rate_limit.triggered with limit_type=rest_step is emitted on 429 step."""
        mock_sink = AsyncMock()
        hub, _app, client = _make_app(telemetry_sink=mock_sink)
        hid = "abcdef12-0000-0000-0000-000000000000"

        with patch.object(hub, "allow_rest_send_for", return_value=False):
            resp = client.post(f"/worker/wkr-3/hijack/{hid}/step")

        assert resp.status_code == 429
        assert resp.json()["error"] == "rate_limited"

        emitted = [c[0][0] for c in mock_sink.emit.call_args_list]
        rate_limited = [e for e in emitted if e.event_type == "rate_limit.triggered"]
        assert rate_limited, "rate_limit.triggered must be emitted on step rate-limit rejection"
        evt = rate_limited[0]
        assert evt.worker_id == "wkr-3"
        assert evt.metadata.get("limit_type") == "rest_step"
        assert evt.metadata.get("client_id") is not None


# ---------------------------------------------------------------------------
# Auth-denial emission — hub_authz.py
# ---------------------------------------------------------------------------


def _make_mock_connection(
    path: str,
    *,
    hub: Any = None,
    authz: Any = None,
    principal: Any = "test-principal",
) -> MagicMock:
    """Build a minimal HTTPConnection mock for _require_hub_route_authz tests."""
    conn = MagicMock()
    conn.scope = {"path": path}
    conn.state.uterm_principal = principal
    conn.app.state.uterm_hub = hub
    conn.app.state.uterm_authz = authz
    return conn


class TestAuthDeniedAdminRequired:
    async def test_admin_required_emits_auth_denied(self) -> None:
        """auth.denied with reason=admin_required is emitted on 403 admin check."""
        from fastapi import HTTPException

        from provide.uterm.server.app.hub_authz import build_require_hub_route_authz

        mock_sink = AsyncMock()
        hub = TermHub(telemetry_sink=mock_sink)

        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=False)

        dep = build_require_hub_route_authz(registry_getter=lambda: None)
        conn = _make_mock_connection("/worker/sess-a/disconnect_worker", hub=hub, authz=authz)

        with pytest.raises(HTTPException) as exc_info:
            await dep(conn)

        assert exc_info.value.status_code == 403

        emitted = [c[0][0] for c in mock_sink.emit.call_args_list]
        denied = [e for e in emitted if e.event_type == "auth.denied"]
        assert denied, "auth.denied must be emitted when admin check fails"
        evt = denied[0]
        assert evt.metadata["reason"] == "admin_required"
        assert evt.metadata["status"] == 403
        assert evt.worker_id == "sess-a"
        assert "test-principal" in evt.principal


class TestAuthDeniedUnknownSession:
    async def test_unknown_session_emits_auth_denied(self) -> None:
        """auth.denied with reason=unknown_session is emitted on 404 unknown session."""
        from fastapi import HTTPException

        from provide.uterm.server.app.hub_authz import build_require_hub_route_authz

        mock_sink = AsyncMock()
        hub = TermHub(telemetry_sink=mock_sink)

        authz = MagicMock()

        # registry returns None for any session
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=None)

        dep = build_require_hub_route_authz(registry_getter=lambda: registry)
        conn = _make_mock_connection("/worker/sess-b/hijack/acquire", hub=hub, authz=authz)

        with pytest.raises(HTTPException) as exc_info:
            await dep(conn)

        assert exc_info.value.status_code == 404

        emitted = [c[0][0] for c in mock_sink.emit.call_args_list]
        denied = [e for e in emitted if e.event_type == "auth.denied"]
        assert denied, "auth.denied must be emitted when session is unknown"
        evt = denied[0]
        assert evt.metadata["reason"] == "unknown_session"
        assert evt.metadata["status"] == 404
        assert evt.worker_id == "sess-b"


class TestAuthDeniedReadDenied:
    async def test_read_denied_emits_auth_denied(self) -> None:
        """auth.denied with reason=read_denied is emitted on 403 read check."""
        from fastapi import HTTPException

        from provide.uterm.server.app.hub_authz import build_require_hub_route_authz

        mock_sink = AsyncMock()
        hub = TermHub(telemetry_sink=mock_sink)

        session_obj = MagicMock()
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=session_obj)

        authz = MagicMock()
        authz.can_read_session = AsyncMock(return_value=False)

        dep = build_require_hub_route_authz(registry_getter=lambda: registry)
        # snapshot path → session.read capability
        conn = _make_mock_connection("/worker/sess-c/hijack/hid-123/snapshot", hub=hub, authz=authz)

        with pytest.raises(HTTPException) as exc_info:
            await dep(conn)

        assert exc_info.value.status_code == 403

        emitted = [c[0][0] for c in mock_sink.emit.call_args_list]
        denied = [e for e in emitted if e.event_type == "auth.denied"]
        assert denied, "auth.denied must be emitted when read access is denied"
        evt = denied[0]
        assert evt.metadata["reason"] == "read_denied"
        assert evt.metadata["status"] == 403
        assert evt.worker_id == "sess-c"


class TestAuthDeniedMutateDenied:
    async def test_mutate_denied_emits_auth_denied(self) -> None:
        """auth.denied with reason=mutate_denied is emitted on 403 mutate check."""
        from fastapi import HTTPException

        from provide.uterm.server.app.hub_authz import build_require_hub_route_authz

        mock_sink = AsyncMock()
        hub = TermHub(telemetry_sink=mock_sink)

        session_obj = MagicMock()
        registry = MagicMock()
        registry.get_definition = AsyncMock(return_value=session_obj)

        authz = MagicMock()
        authz.can_mutate_session = AsyncMock(return_value=False)

        dep = build_require_hub_route_authz(registry_getter=lambda: registry)
        # acquire path → session.control.hijack capability (mutate branch)
        conn = _make_mock_connection("/worker/sess-d/hijack/acquire", hub=hub, authz=authz)

        with pytest.raises(HTTPException) as exc_info:
            await dep(conn)

        assert exc_info.value.status_code == 403

        emitted = [c[0][0] for c in mock_sink.emit.call_args_list]
        denied = [e for e in emitted if e.event_type == "auth.denied"]
        assert denied, "auth.denied must be emitted when mutate access is denied"
        evt = denied[0]
        assert evt.metadata["reason"] == "mutate_denied"
        assert evt.metadata["status"] == 403
        assert evt.worker_id == "sess-d"


class TestEmitDeniedNoHubBranch:
    async def test_emit_denied_noop_when_no_hub_on_app_state(self) -> None:
        """_emit_denied silently skips emission when uterm_hub is absent from app.state.

        Covers the ``if _hub is not None`` branch (False path) in _emit_denied.
        """
        from fastapi import HTTPException

        from provide.uterm.server.app.hub_authz import build_require_hub_route_authz

        authz = MagicMock()
        authz.is_admin = AsyncMock(return_value=False)

        dep = build_require_hub_route_authz(registry_getter=lambda: None)

        conn = MagicMock()
        conn.scope = {"path": "/worker/sess-e/disconnect_worker"}
        conn.state.uterm_principal = "some-principal"
        # uterm_hub absent — getattr returns None
        type(conn.app.state).uterm_hub = MagicMock()
        conn.app.state.uterm_hub = None
        conn.app.state.uterm_authz = authz

        # Should still raise 403 but NOT error on emit
        with pytest.raises(HTTPException) as exc_info:
            await dep(conn)
        assert exc_info.value.status_code == 403
