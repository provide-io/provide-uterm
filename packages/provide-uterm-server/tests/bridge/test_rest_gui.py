#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for the ``/gui/`` REST route handlers (rest_gui).

Each endpoint is invoked directly with a mocked hub + a minimal fake request,
mirroring ``test_rest_lease_ownership``. Session-level authz is applied by the
``hub_authz`` dependency (tested separately); these tests cover the handler
mechanics + branches: attach (capability / target_id / tenant scope / unknown
target / rfb-unreachable / memory success), screenshot (no-session / no-graphical /
PNG), and click/type/key/drag (no-session / no-graphical / injection).
"""

from __future__ import annotations

import base64
import json
import struct
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from provide.uterm.server.bridge.hub.registry import WorkerRegistry
from provide.uterm.server.bridge.models import WorkerTermState
from provide.uterm.server.bridge.routes import rest_gui
from provide.uterm.server.bridge.routes.rest_gui import register_gui_routes
from provide.uterm.server.graphical_targets import (
    GraphicalTargetDefinition,
    InMemoryGraphicalTargetRegistry,
)
from provide.uterm.server.gui_session import MemoryGraphicalSession

WID = "gui-worker"
HID = "00000000-0000-0000-0000-000000000000"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Minimal stand-in for a Starlette Request the handlers touch."""

    def __init__(
        self,
        *,
        principal: Any = None,
        authz: Any = None,
        targets: Any = None,
        body: Any = None,
        bad_json: bool = False,
    ) -> None:
        self.state = SimpleNamespace(uterm_principal=principal)
        self.app = SimpleNamespace(state=SimpleNamespace(uterm_authz=authz, uterm_graphical_targets=targets))
        self._body = {} if body is None else body
        self._bad_json = bad_json

    async def json(self) -> Any:
        if self._bad_json:
            raise ValueError("bad json")
        return self._body


def _hub(*, rest_session: Any = "MISSING", worker_state: WorkerTermState | None = None) -> Any:
    registry = WorkerRegistry()
    if worker_state is not None:
        registry.put(WID, worker_state)
    hub = SimpleNamespace()
    # Real HijackSessions carry acquired_by (None for legacy/unauthenticated
    # leases); _require_graphical_session reads it for principal-bound inject.
    session = (
        SimpleNamespace(lease_expires_at=time.monotonic() + 60, acquired_by=None)
        if rest_session == "MISSING"
        else rest_session
    )
    hub.get_rest_session = AsyncMock(return_value=session)
    hub.registry = registry
    return hub


def _endpoint(hub: Any, path: str) -> Any:
    router = APIRouter()
    register_gui_routes(hub, router)
    for route in router.routes:
        if getattr(route, "path", "") == path:
            return route.endpoint  # type: ignore[attr-defined]
    raise AssertionError(f"route not found: {path}")


def _seeded_targets() -> InMemoryGraphicalTargetRegistry:
    reg = InMemoryGraphicalTargetRegistry()
    reg.add_static(
        GraphicalTargetDefinition(target_id="gt-mem", tenant_id="acme", protocol="memory", width=10, height=8)
    )
    reg.add_static(
        GraphicalTargetDefinition(target_id="gt-rfb", tenant_id="acme", protocol="rfb", endpoint="127.0.0.1:1")
    )
    return reg


def _principal(tenant_id: str | None = "acme") -> SimpleNamespace:
    return SimpleNamespace(tenant_id=tenant_id, subject_id="u1")


def _authz(*, allow: bool = True) -> SimpleNamespace:
    return SimpleNamespace(has_capability=AsyncMock(return_value=allow))


def _status(resp: Any) -> int:
    assert isinstance(resp, JSONResponse)
    return resp.status_code


def _body(resp: JSONResponse) -> Any:
    return json.loads(resp.body)


ATTACH = "/worker/{worker_id}/gui/attach"
SHOT = "/worker/{worker_id}/hijack/{hijack_id}/gui/screenshot"
CLICK = "/worker/{worker_id}/hijack/{hijack_id}/gui/click"
TYPE = "/worker/{worker_id}/hijack/{hijack_id}/gui/type"
KEY = "/worker/{worker_id}/hijack/{hijack_id}/gui/key"
DRAG = "/worker/{worker_id}/hijack/{hijack_id}/gui/drag"


# ---------------------------------------------------------------------------
# attach
# ---------------------------------------------------------------------------


class TestAttach:
    async def test_capability_denied(self) -> None:
        hub = _hub()
        ep = _endpoint(hub, ATTACH)
        req = _FakeRequest(principal=_principal(), authz=_authz(allow=False), targets=_seeded_targets())
        resp = await ep(req, WID)
        assert _status(resp) == 403
        assert _body(resp)["error"] == "insufficient privileges"

    async def test_missing_target_id(self) -> None:
        hub = _hub()
        ep = _endpoint(hub, ATTACH)
        req = _FakeRequest(principal=_principal(), authz=_authz(), targets=_seeded_targets(), body={})
        resp = await ep(req, WID)
        assert _status(resp) == 422
        assert "target_id is required" in _body(resp)["error"]

    async def test_tenant_scope_denied(self) -> None:
        hub = _hub()
        ep = _endpoint(hub, ATTACH)
        req = _FakeRequest(
            principal=_principal(tenant_id=""),
            authz=_authz(),
            targets=_seeded_targets(),
            body={"target_id": "gt-mem"},
        )
        resp = await ep(req, WID)
        assert _status(resp) == 403
        assert _body(resp)["error"] == "graphical target access denied"

    async def test_unknown_target(self) -> None:
        hub = _hub()
        ep = _endpoint(hub, ATTACH)
        req = _FakeRequest(
            principal=_principal(), authz=_authz(), targets=_seeded_targets(), body={"target_id": "nope"}
        )
        resp = await ep(req, WID)
        assert _status(resp) == 404
        assert _body(resp)["error"] == "target not found"

    async def test_rfb_console_that_is_not_listening_is_a_bad_gateway(self) -> None:
        """rfb attaches for real now; an unreachable console is 502, not 501.

        This asserted 501 "graphical protocol not supported: rfb" until the RFB
        client landed. 501 now means what it says — a protocol this server has
        no client for — and a registered target whose console is down is a
        gateway failure, matching the C# canonical's "rfb connect failed".
        """
        hub = _hub()
        ep = _endpoint(hub, ATTACH)
        req = _FakeRequest(
            principal=_principal(), authz=_authz(), targets=_seeded_targets(), body={"target_id": "gt-rfb"}
        )
        resp = await ep(req, WID)
        assert _status(resp) == 502
        assert _body(resp)["error"].startswith("rfb connect failed:")

    async def test_a_cloud_metadata_console_is_refused_before_the_dial(self) -> None:
        """The egress guard runs first, so a target cannot name 169.254.169.254.

        block_private_connector_targets is off by default — reaching an internal
        console is the point — but cloud-metadata is blocked either way, which
        is what stops a tenant turning a graphical target into a credential
        read. Matches EgressGuard in UtermServer.Gui.cs.
        """
        registry = _seeded_targets()
        registry.add_static(
            GraphicalTargetDefinition(
                target_id="gt-meta", tenant_id="acme", protocol="rfb", endpoint="169.254.169.254:5900"
            )
        )
        ep = _endpoint(_hub(), ATTACH)
        req = _FakeRequest(principal=_principal(), authz=_authz(), targets=registry, body={"target_id": "gt-meta"})
        resp = await ep(req, WID)
        assert _status(resp) == 403
        assert _body(resp)["error"].startswith("invalid endpoint:")

    async def test_memory_success_creates_worker_state(self) -> None:
        hub = _hub()  # no worker state pre-registered
        ep = _endpoint(hub, ATTACH)
        req = _FakeRequest(
            principal=_principal(), authz=_authz(), targets=_seeded_targets(), body={"target_id": "gt-mem"}
        )
        resp = await ep(req, WID)
        assert resp == {"ok": True, "target_id": "gt-mem"}
        st = hub.registry.get(WID)
        assert st is not None
        assert isinstance(st.graphical_session, MemoryGraphicalSession)
        assert st.graphical_session.screenshot().width == 10

    async def test_memory_success_reuses_existing_worker_state(self) -> None:
        existing = WorkerTermState(input_mode="open")
        hub = _hub(worker_state=existing)
        ep = _endpoint(hub, ATTACH)
        req = _FakeRequest(
            principal=_principal(), authz=_authz(), targets=_seeded_targets(), body={"target_id": "gt-mem"}
        )
        resp = await ep(req, WID)
        assert resp == {"ok": True, "target_id": "gt-mem"}
        # The pre-existing state object is mutated in place (not replaced).
        assert hub.registry.get(WID) is existing
        assert existing.input_mode == "open"
        assert isinstance(existing.graphical_session, MemoryGraphicalSession)

    async def test_a_protocol_with_no_client_is_not_implemented(self) -> None:
        """litevirt is a registrable protocol this server has no client for.

        The registry accepts memory/rfb/litevirt (SUPPORTED_PROTOCOLS), so a
        litevirt target can exist and be attached to; only Go speaks litevirt
        today. 501 is the honest answer — the target is valid, the server is
        the one that falls short — and it mirrors C#'s 501 for the same
        protocol.
        """
        registry = _seeded_targets()
        registry.add_static(
            GraphicalTargetDefinition(
                target_id="gt-lv", tenant_id="acme", protocol="litevirt", endpoint="127.0.0.1:5900"
            )
        )
        ep = _endpoint(_hub(), ATTACH)
        req = _FakeRequest(principal=_principal(), authz=_authz(), targets=registry, body={"target_id": "gt-lv"})
        resp = await ep(req, WID)
        assert _status(resp) == 501
        assert _body(resp)["error"] == "graphical protocol not supported: litevirt"

    async def test_attaching_again_closes_the_session_it_replaces(self) -> None:
        """A replaced session must be closed, or its socket + reader thread leak.

        MemoryGraphicalSession has nothing to release, but an RfbGraphicalSession
        holds a TCP socket and a daemon reader thread; re-attaching a worker
        without closing the old one leaks both for the life of the process.
        """
        closed: list[str] = []
        existing = WorkerTermState()
        existing.graphical_session = SimpleNamespace(close=lambda: closed.append("closed"))  # type: ignore[assignment]
        hub = _hub(worker_state=existing)
        ep = _endpoint(hub, ATTACH)
        req = _FakeRequest(
            principal=_principal(), authz=_authz(), targets=_seeded_targets(), body={"target_id": "gt-mem"}
        )
        resp = await ep(req, WID)
        assert resp == {"ok": True, "target_id": "gt-mem"}
        assert closed == ["closed"]
        assert isinstance(existing.graphical_session, MemoryGraphicalSession)

    async def test_a_replaced_session_that_cannot_close_still_attaches(self) -> None:
        """Closing the old session is best-effort: the new attach still wins.

        A dead console's close() raises (the socket is already gone). Failing
        the attach on that would leave the worker holding the session that just
        proved itself broken.
        """

        def _boom() -> None:
            raise OSError("socket already gone")

        existing = WorkerTermState()
        existing.graphical_session = SimpleNamespace(close=_boom)  # type: ignore[assignment]
        hub = _hub(worker_state=existing)
        ep = _endpoint(hub, ATTACH)
        req = _FakeRequest(
            principal=_principal(), authz=_authz(), targets=_seeded_targets(), body={"target_id": "gt-mem"}
        )
        resp = await ep(req, WID)
        assert resp == {"ok": True, "target_id": "gt-mem"}
        assert isinstance(existing.graphical_session, MemoryGraphicalSession)

    async def test_bad_json_body_treated_as_missing_target(self) -> None:
        hub = _hub()
        ep = _endpoint(hub, ATTACH)
        req = _FakeRequest(principal=_principal(), authz=_authz(), targets=_seeded_targets(), bad_json=True)
        resp = await ep(req, WID)
        assert _status(resp) == 422


# ---------------------------------------------------------------------------
# screenshot
# ---------------------------------------------------------------------------


class TestScreenshot:
    async def test_no_hijack_session(self) -> None:
        hub = _hub(rest_session=None)
        ep = _endpoint(hub, SHOT)
        resp = await ep(WID, HID)
        assert _status(resp) == 404
        assert _body(resp)["error"] == "Invalid or expired hijack session."

    async def test_no_graphical_session_unattached_worker(self) -> None:
        hub = _hub(worker_state=WorkerTermState())  # worker present, graphical_session None
        ep = _endpoint(hub, SHOT)
        resp = await ep(WID, HID)
        assert _status(resp) == 404
        assert _body(resp)["error"] == "No graphical session attached."

    async def test_no_graphical_session_unknown_worker(self) -> None:
        # Active hijack lease but the worker has no registry state at all
        # (exercises the ``st is None`` branch of ``_graphical_session``).
        hub = _hub(worker_state=None)
        ep = _endpoint(hub, SHOT)
        resp = await ep(WID, HID)
        assert _status(resp) == 404
        assert _body(resp)["error"] == "No graphical session attached."

    async def test_success_returns_png(self) -> None:
        st = WorkerTermState(graphical_session=MemoryGraphicalSession(4, 3))
        hub = _hub(worker_state=st)
        ep = _endpoint(hub, SHOT)
        resp = await ep(WID, HID)
        assert resp["ok"] is True
        assert resp["worker_id"] == WID
        assert resp["hijack_id"] == HID
        assert resp["lease_expires_at"] > time.time()
        png = base64.b64decode(resp["screenshot"])
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        w, h = struct.unpack(">II", png[16:24])
        assert (w, h) == (4, 3)


# ---------------------------------------------------------------------------
# click / type / key / drag
# ---------------------------------------------------------------------------


def _attached_hub() -> tuple[Any, MemoryGraphicalSession]:
    sess = MemoryGraphicalSession(20, 20)
    hub = _hub(worker_state=WorkerTermState(graphical_session=sess))
    return hub, sess


class TestInputGuards:
    @pytest.mark.parametrize("path", [CLICK, TYPE, KEY, DRAG])
    async def test_no_hijack_session(self, path: str) -> None:
        hub = _hub(rest_session=None)
        ep = _endpoint(hub, path)
        resp = await ep(_FakeRequest(), WID, HID)
        assert _status(resp) == 404
        assert _body(resp)["error"] == "Invalid or expired hijack session."

    @pytest.mark.parametrize("path", [CLICK, TYPE, KEY, DRAG])
    async def test_no_graphical_session(self, path: str) -> None:
        hub = _hub(worker_state=WorkerTermState())
        ep = _endpoint(hub, path)
        resp = await ep(_FakeRequest(), WID, HID)
        assert _status(resp) == 404
        assert _body(resp)["error"] == "No graphical session attached."


class TestClick:
    async def test_left_click_sets_pixel(self) -> None:
        hub, sess = _attached_hub()
        ep = _endpoint(hub, CLICK)
        resp = await ep(_FakeRequest(body={"x": 3, "y": 2, "button": "left"}), WID, HID)
        assert resp == {"ok": True}
        idx = ((2 * 20) + 3) * 4
        assert sess.screenshot().pixels[idx] == 255

    async def test_unknown_button_rejected(self) -> None:
        # Unknown buttons are rejected (not silently coerced to left).
        hub, _sess = _attached_hub()
        ep = _endpoint(hub, CLICK)
        resp = await ep(_FakeRequest(body={"x": 1, "y": 1, "button": "wheel"}), WID, HID)
        assert _status(resp) == 422
        assert "invalid button" in _body(resp)["error"]

    async def test_middle_button_is_noop_on_framebuffer(self) -> None:
        # Middle (mask 2) has no bit-0 set → MemoryGraphicalSession ignores it.
        hub, sess = _attached_hub()
        ep = _endpoint(hub, CLICK)
        await ep(_FakeRequest(body={"x": 1, "y": 1, "button": "middle"}), WID, HID)
        assert sess.screenshot().pixels == bytearray(20 * 20 * 4)

    async def test_string_and_default_coords(self) -> None:
        hub, sess = _attached_hub()
        ep = _endpoint(hub, CLICK)
        # x as numeric string, y absent (default 0).
        resp = await ep(_FakeRequest(body={"x": "5"}), WID, HID)
        assert resp == {"ok": True}
        idx = ((0 * 20) + 5) * 4
        assert sess.screenshot().pixels[idx] == 255


class TestType:
    async def test_types_each_char(self) -> None:
        hub, sess = _attached_hub()
        ep = _endpoint(hub, TYPE)
        resp = await ep(_FakeRequest(body={"text": "hi"}), WID, HID)
        assert resp == {"ok": True}

    async def test_empty_text(self) -> None:
        hub, _ = _attached_hub()
        ep = _endpoint(hub, TYPE)
        resp = await ep(_FakeRequest(body={}), WID, HID)
        assert resp == {"ok": True}


class TestKey:
    async def test_known_key(self) -> None:
        hub, _ = _attached_hub()
        ep = _endpoint(hub, KEY)
        resp = await ep(_FakeRequest(body={"key_name": "Enter"}), WID, HID)
        assert resp == {"ok": True}

    async def test_unknown_key_defaults_zero(self) -> None:
        hub, _ = _attached_hub()
        ep = _endpoint(hub, KEY)
        resp = await ep(_FakeRequest(body={"key_name": "F13"}), WID, HID)
        assert resp == {"ok": True}


class TestDrag:
    async def test_drag_paints_endpoint(self) -> None:
        hub, sess = _attached_hub()
        ep = _endpoint(hub, DRAG)
        resp = await ep(_FakeRequest(body={"start_x": 0, "start_y": 0, "end_x": 4, "end_y": 5}), WID, HID)
        assert resp == {"ok": True}
        # start (0,0) and end (4,5) both painted during the button-down phase.
        assert sess.screenshot().pixels[0] == 255
        end_idx = ((5 * 20) + 4) * 4
        assert sess.screenshot().pixels[end_idx] == 255


# ---------------------------------------------------------------------------
# body-field helpers
# ---------------------------------------------------------------------------


class TestFieldHelpers:
    async def test_read_json_non_dict_is_empty(self) -> None:
        assert await rest_gui._read_json_object(_FakeRequest(body=[1, 2, 3])) == {}

    async def test_read_json_bad_is_empty(self) -> None:
        assert await rest_gui._read_json_object(_FakeRequest(bad_json=True)) == {}

    def test_int_field_variants(self) -> None:
        assert rest_gui._int_field({"a": 7}, "a") == 7
        assert rest_gui._int_field({"a": "9"}, "a") == 9
        assert rest_gui._int_field({"a": "x"}, "a", 3) == 3
        assert rest_gui._int_field({"a": True}, "a", 3) == 3
        assert rest_gui._int_field({"a": 1.5}, "a", 3) == 3
        assert rest_gui._int_field({}, "a", 3) == 3

    def test_str_field_variants(self) -> None:
        assert rest_gui._str_field({"a": "hi"}, "a") == "hi"
        assert rest_gui._str_field({"a": 5}, "a", "d") == "d"
        assert rest_gui._str_field({}, "a", "d") == "d"
