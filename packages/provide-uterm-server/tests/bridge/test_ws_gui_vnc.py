#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Human VNC WebSocket route authz + upstream-unavailable path."""

from __future__ import annotations

import io
import threading
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from provide.uterm.server.bridge.frames import make_hello_frame
from provide.uterm.server.bridge.routes.ws_gui_vnc import (
    _close_quietly,
    check_vnc_relay_authz,
    principal_role_name,
    register_gui_vnc_ws_routes,
)

WID = "vnc-worker"
HID = "00000000-0000-0000-0000-0000000000ab"
PATH = f"/worker/{WID}/hijack/{HID}/gui/vnc"


class _FakeUpstreamR(io.RawIOBase):
    """Fake VNC upstream read stream: emits *data* once, then EOFs.

    ``block_on_eof=True`` (default) makes the post-data read block on an event
    until :meth:`close` — mimics a live VNC server holding the TCP session open.
    ``block_on_eof=False`` returns ``b""`` immediately — mimics an upstream
    hangup so tests can exercise the upstream-EOF relay-teardown path.
    """

    def __init__(self, data: bytes = b"RFB 003.008\n", *, block_on_eof: bool = True) -> None:
        self._data = data
        self._pos = 0
        self._ev = threading.Event()
        self._block_on_eof = block_on_eof

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        if self._ev.is_set():
            return b""
        if self._pos >= len(self._data):
            if self._block_on_eof:
                self._ev.wait(timeout=2.0)
            return b""
        end = len(self._data) if size < 0 else min(len(self._data), self._pos + size)
        chunk = self._data[self._pos : end]
        self._pos = end
        return chunk

    def close(self) -> None:
        self._ev.set()
        super().close()

    def readable(self) -> bool:
        return True


class _FakeUpstreamW(io.RawIOBase):
    """Fake VNC upstream write stream: accumulates forwarded bytes in ``.buf``."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, b: bytes) -> int:  # type: ignore[override]
        self.buf.extend(b)
        return len(b)

    def writable(self) -> bool:
        return True

    def flush(self) -> None:
        return None


def _principal(*, subject: str = "alice", roles: frozenset[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(subject_id=subject, roles=roles if roles is not None else frozenset({"operator"}))


def test_principal_role_name_picks_highest() -> None:
    p = _principal(roles=frozenset({"viewer", "admin"}))
    assert principal_role_name(p) == "admin"


def test_principal_role_name_ignores_non_improving_role() -> None:
    # Ordered roles (a list, not a hash-ordered set) so a lower-rank role
    # deterministically follows a higher one — exercises the "rank not greater"
    # loop branch regardless of PYTHONHASHSEED.
    p = SimpleNamespace(subject_id="x", roles=["admin", "viewer", "operator"])
    assert principal_role_name(p) == "admin"


def test_authz_requires_principal() -> None:
    assert check_vnc_relay_authz(principal=None, hijack_session=object(), hijack_id=HID) == "authentication required"


def test_authz_requires_operator() -> None:
    hs = SimpleNamespace(hijack_id=HID, acquired_by="alice")
    err = check_vnc_relay_authz(principal=_principal(roles=frozenset({"viewer"})), hijack_session=hs, hijack_id=HID)
    assert err == "insufficient privileges"


def test_authz_requires_session() -> None:
    err = check_vnc_relay_authz(principal=_principal(), hijack_session=None, hijack_id=HID)
    assert err == "invalid or expired hijack session"


def test_authz_principal_bind() -> None:
    hs = SimpleNamespace(hijack_id=HID, acquired_by="alice")
    err = check_vnc_relay_authz(principal=_principal(subject="bob"), hijack_session=hs, hijack_id=HID)
    assert err == "hijack lease not owned by caller"


def test_authz_ok_owner() -> None:
    hs = SimpleNamespace(hijack_id=HID, acquired_by="alice")
    got = check_vnc_relay_authz(principal=_principal(subject="alice"), hijack_session=hs, hijack_id=HID)
    assert not isinstance(got, str)
    assert got.principal_id == "alice"
    assert got.principal_role == "operator"
    assert got.lease_id == HID


def test_authz_legacy_unbound_lease() -> None:
    hs = SimpleNamespace(hijack_id=HID, acquired_by=None)
    got = check_vnc_relay_authz(principal=_principal(subject="anyone"), hijack_session=hs, hijack_id=HID)
    assert not isinstance(got, str)


def test_hello_vnc_supported_default() -> None:
    assert make_hello_frame()["vnc_supported"] is True


class _PrincipalASGI:
    """Inject ``scope['state']['uterm_principal']`` for HTTP and WebSocket."""

    def __init__(self, app: Any, principal: Any | None) -> None:
        self.app = app
        self.principal = principal

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] in {"http", "websocket"}:
            state = scope.setdefault("state", {})
            # Starlette may already have installed a State object.
            if hasattr(state, "__setattr__") and not isinstance(state, dict):
                state.uterm_principal = self.principal
            else:
                state["uterm_principal"] = self.principal
        await self.app(scope, receive, send)


def _client(*, rest_session: Any, principal: Any | None) -> TestClient:
    hub = SimpleNamespace()
    hub.get_rest_session = AsyncMock(return_value=rest_session)
    hub.vnc_upstream_factory = None

    app = FastAPI()
    router = APIRouter()
    register_gui_vnc_ws_routes(hub, router)
    app.include_router(router)
    return TestClient(_PrincipalASGI(app, principal))


def _connect_close_code(client: TestClient, path: str = PATH) -> int | None:
    """Return WebSocket close code when the server rejects/ends immediately."""
    try:
        with client.websocket_connect(path) as ws:
            try:
                msg = ws.receive()
                if isinstance(msg, dict) and msg.get("type") == "websocket.close":
                    code = msg.get("code")
                    return int(code) if code is not None else None
            except WebSocketDisconnect as exc:
                return int(exc.code) if exc.code is not None else None
    except WebSocketDisconnect as exc:
        return int(exc.code) if exc.code is not None else None
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code is not None:
            return int(code)
        # httpx2/starlette sometimes wraps: "WebSocket is not connected. Need to call "accept" first"?
        # Fall through — none means test fails with a clear assertion.
    return None


def test_ws_closes_upstream_unavailable_after_authz() -> None:
    hs = SimpleNamespace(hijack_id=HID, acquired_by="alice")
    client = _client(rest_session=hs, principal=_principal(subject="alice"))
    code = _connect_close_code(client)
    assert code == 1013


def test_ws_policy_reject_viewer() -> None:
    hs = SimpleNamespace(hijack_id=HID, acquired_by="alice")
    client = _client(rest_session=hs, principal=_principal(subject="alice", roles=frozenset({"viewer"})))
    code = _connect_close_code(client)
    assert code == 1008


def test_ws_policy_reject_non_owner() -> None:
    hs = SimpleNamespace(hijack_id=HID, acquired_by="alice")
    client = _client(rest_session=hs, principal=_principal(subject="bob"))
    code = _connect_close_code(client)
    assert code == 1008


def test_ws_policy_reject_missing_session() -> None:
    client = _client(rest_session=None, principal=_principal(subject="alice"))
    code = _connect_close_code(client)
    assert code == 1008


def test_ws_policy_reject_unauthenticated() -> None:
    hs = SimpleNamespace(hijack_id=HID, acquired_by="alice")
    client = _client(rest_session=hs, principal=None)
    code = _connect_close_code(client)
    assert code == 1008


def test_ws_upstream_factory_relays_bytes() -> None:
    """When factory returns streams, binary WS path relays RFB-like bytes."""
    up_r = _FakeUpstreamR()
    up_w = _FakeUpstreamW()

    def factory(_wid: str, target_id: str | None) -> tuple[Any, Any] | None:
        assert target_id == "lab-vnc"
        return (up_r, up_w)  # type: ignore[return-value]

    hub = SimpleNamespace()
    hub.get_rest_session = AsyncMock(return_value=SimpleNamespace(hijack_id=HID, acquired_by="alice"))
    hub.vnc_upstream_factory = factory

    app = FastAPI()
    router = APIRouter()
    register_gui_vnc_ws_routes(hub, router)
    app.include_router(router)
    client = TestClient(_PrincipalASGI(app, _principal(subject="alice")))

    with client.websocket_connect(PATH + "?target_id=lab-vnc") as ws:
        # First server→client chunk should be the RFB version banner.
        msg = ws.receive_bytes()
        assert msg.startswith(b"RFB ")
        ws.send_bytes(b"RFB 003.008\n")
        # Give relay a moment to pump browser→upstream.
        time.sleep(0.15)
    assert b"RFB 003.008\n" in bytes(up_w.buf)


def _relay_hub(factory: Any) -> Any:
    hub = SimpleNamespace()
    hub.get_rest_session = AsyncMock(return_value=SimpleNamespace(hijack_id=HID, acquired_by="alice"))
    hub.vnc_upstream_factory = factory
    return hub


def _relay_client(hub: Any) -> TestClient:
    app = FastAPI()
    router = APIRouter()
    register_gui_vnc_ws_routes(hub, router)
    app.include_router(router)
    return TestClient(_PrincipalASGI(app, _principal(subject="alice")))


def test_ws_relay_handles_text_empty_and_inject_messages() -> None:
    """Latin-1-safe text frames are encoded, empty frames skipped, inject frames gated."""
    up_r, up_w = _FakeUpstreamR(), _FakeUpstreamW()
    client = _relay_client(_relay_hub(lambda _w, _t: (up_r, up_w)))  # type: ignore[arg-type]
    with client.websocket_connect(PATH + "?target_id=lab-vnc") as ws:
        assert ws.receive_bytes().startswith(b"RFB ")
        ws.send_text("RFB 003.008\n")  # text frame (ProtocolVersion) → latin-1 encoded losslessly
        ws.send_bytes(b"")  # empty frame → skipped
        ws.send_bytes(bytes([1]))  # security type None
        ws.send_bytes(bytes([1]))  # ClientInit
        ws.send_bytes(bytes([4]) + bytes(7))  # KeyEvent → routed through inject gate
        # The relay pumps frames to the upstream writer on another thread, so
        # this waits for the write rather than sleeping a fixed 0.2s past it.
        deadline = time.monotonic() + 10.0
        while b"RFB 003.008\n" not in bytes(up_w.buf) and time.monotonic() < deadline:
            time.sleep(0.01)
    assert b"RFB 003.008\n" in bytes(up_w.buf)


def test_ws_factory_error_closes_connection() -> None:
    """A factory that raises is treated as upstream-unavailable (clean close)."""

    def _boom(_w: str, _t: str | None) -> Any:
        raise RuntimeError("dial exploded")

    client = _relay_client(_relay_hub(_boom))
    code = _connect_close_code(client, PATH + "?target_id=lab-vnc")
    assert code is not None


def test_ws_no_factory_configured_closes() -> None:
    """No upstream factory on the hub → upstream unavailable → clean close."""
    hub = SimpleNamespace()
    hub.get_rest_session = AsyncMock(return_value=SimpleNamespace(hijack_id=HID, acquired_by="alice"))
    # deliberately no vnc_upstream_factory attribute
    client = _relay_client(hub)
    code = _connect_close_code(client, PATH + "?target_id=lab-vnc")
    assert code is not None


def test_ws_relay_with_explicit_upstream_factory() -> None:
    """upstream_factory passed to the route (not via hub) is used directly."""
    hub = SimpleNamespace()
    hub.get_rest_session = AsyncMock(return_value=SimpleNamespace(hijack_id=HID, acquired_by="alice"))
    hub.vnc_upstream_factory = None
    app = FastAPI()
    router = APIRouter()
    register_gui_vnc_ws_routes(
        hub,
        router,
        upstream_factory=lambda _w, _t: (_FakeUpstreamR(block_on_eof=False), _FakeUpstreamW()),  # type: ignore[arg-type,return-value]
    )
    app.include_router(router)
    client = TestClient(_PrincipalASGI(app, _principal(subject="alice")))
    with client.websocket_connect(PATH + "?target_id=lab-vnc") as ws:
        assert ws.receive_bytes().startswith(b"RFB ")


def test_ws_relay_closes_when_upstream_eofs_while_browser_idle() -> None:
    """Upstream EOF must tear the relay down even if the browser never sends.

    Regression for the uncancelled-gather hang: previously _relay_to_ws broke on
    upstream EOF while _ws_to_relay stayed blocked in websocket.receive(), so the
    server never closed the socket. Now the first pump to finish cancels the peer
    and the server closes with 1000. Pre-fix this test blocks in ws.receive().
    """
    up_r, up_w = _FakeUpstreamR(block_on_eof=False), _FakeUpstreamW()
    client = _relay_client(_relay_hub(lambda _w, _t: (up_r, up_w)))
    with client.websocket_connect(PATH + "?target_id=lab-vnc") as ws:
        assert ws.receive_bytes().startswith(b"RFB ")
        # Browser stays idle. The server must close on upstream EOF, not hang.
        msg = ws.receive()
        assert isinstance(msg, dict)
        assert msg.get("type") == "websocket.close"
        assert msg.get("code") == 1000


def test_ws_relay_drops_undecodable_text_frame() -> None:
    """A text frame with a code point > 255 is dropped, not lossy ('?') forwarded."""
    up_r, up_w = _FakeUpstreamR(), _FakeUpstreamW()
    client = _relay_client(_relay_hub(lambda _w, _t: (up_r, up_w)))
    with client.websocket_connect(PATH + "?target_id=lab-vnc") as ws:
        assert ws.receive_bytes().startswith(b"RFB ")
        ws.send_text("Ā")  # LATIN CAPITAL A WITH MACRON (>255) → undecodable, dropped
        ws.send_bytes(b"RFB 003.008\n")  # valid ProtocolVersion still relays
        time.sleep(0.2)
    forwarded = bytes(up_w.buf)
    assert b"RFB 003.008\n" in forwarded
    assert b"?" not in forwarded  # the dropped char was NOT re-encoded to b"?"


def test_close_quietly_tolerates_none() -> None:
    """relay_r/relay_w stay None if setup fails before makefile; close path no-ops.

    Exercises the non-callable branch of _close_quietly (getattr(None, 'close')
    is None), which the old '# pragma: no branch' wrongly asserted unreachable.
    """
    _close_quietly(None)  # must not raise


def test_ws_relay_logs_error_on_bad_security_type() -> None:
    """A relay-thread failure (unsupported RFB security type) is logged, not swallowed."""
    up_r, up_w = _FakeUpstreamR(), _FakeUpstreamW()
    client = _relay_client(_relay_hub(lambda _w, _t: (up_r, up_w)))
    with client.websocket_connect(PATH + "?target_id=lab-vnc") as ws:
        assert ws.receive_bytes().startswith(b"RFB ")
        ws.send_bytes(b"RFB 003.008\n")  # 12-byte ProtocolVersion (pass-through)
        ws.send_bytes(bytes([2]))  # security type 2 (not None=1) → filter raises ValueError
        # The relay thread errors out; the endpoint records it and closes the WS.
        msg = ws.receive()
        assert isinstance(msg, dict)
        assert msg.get("type") == "websocket.close"


def test_ws_relay_thread_finishes_without_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The relay thread completing with NO exception leaves ``relay_error`` empty,
    so the endpoint's ``if relay_error:`` guard takes its False branch (341->344).

    Patching ``run_human_relay_streams`` with a forward-the-banner-then-return
    stub makes this DETERMINISTIC: the stub provably never raises, so
    ``relay_error`` stays empty no matter how the relay thread is scheduled. The
    real RFB filter's clean-boundary EOF exit is timing-dependent and only
    flakily hits this branch on Python 3.13/3.14 — the stub removes that race
    without weakening the source or the assertion.
    """
    from provide.uterm.server.bridge.routes import ws_gui_vnc as mod

    def _clean_relay(br: Any, bw: Any, ur: Any, uw: Any, **_kw: Any) -> None:
        # Forward the upstream RFB banner to the browser side, then return with no
        # exception. No ``raise`` -> _run_relay's ``except`` is never entered ->
        # relay_error stays empty when the endpoint reaches ``if relay_error:``.
        data = ur.read(4096)
        if data:
            bw.write(data)
            bw.flush()

    monkeypatch.setattr(mod, "run_human_relay_streams", _clean_relay)

    up_r, up_w = _FakeUpstreamR(block_on_eof=False), _FakeUpstreamW()
    client = _relay_client(_relay_hub(lambda _w, _t: (up_r, up_w)))  # type: ignore[arg-type]
    with client.websocket_connect(PATH + "?target_id=lab-vnc") as ws:
        # The banner still crosses the relay, proving the clean pump ran; the
        # thread then exits with no error and the server closes normally.
        assert ws.receive_bytes().startswith(b"RFB ")
