#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Human VNC WebSocket relay route for the hijack hub.

Registers::

    WS /worker/{worker_id}/hijack/{hijack_id}/gui/vnc?target_id=…

Authz mirrors GUI inject: authenticated principal, operator+/admin role
(``session.control.hijack``), live REST hijack session, and ``acquired_by``
principal bind. Browser→upstream RFB input is gated by
:func:`provide.uterm.bridge.policy.can_inject` via
:func:`provide.uterm.vnc.run_human_relay_streams`.

Without an upstream duplex factory (RFB dial not wired), the socket is
accepted then closed with 1013 ``upstream unavailable`` after a successful
authz path — so unit tests can assert the gate without a real VNC backend.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, BinaryIO

try:
    from fastapi import APIRouter, Path, Query, WebSocket, WebSocketDisconnect
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for hijack routes: pip install 'provide-uterm[websocket]'") from _e

from provide.telemetry import get_logger
from provide.uterm.bridge.policy import can_inject
from provide.uterm.vnc import run_human_relay_streams

if TYPE_CHECKING:
    from provide.uterm.server.bridge.hub import TermHub

logger = get_logger(__name__)

# Close codes: 1008 policy; 1013 try-again / upstream unavailable (RFC 6455).
_CLOSE_POLICY = 1008
_CLOSE_UPSTREAM = 1013
_CLOSE_NORMAL = 1000

# (worker_id, target_id | None) -> (upstream_r, upstream_w) or None if unavailable.
UpstreamDuplexFactory = Callable[[str, str | None], tuple[BinaryIO, BinaryIO] | None]

_ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}


@dataclass(frozen=True, slots=True)
class VncRelayAuth:
    """Resolved principal + lease context for a human VNC relay."""

    session_id: str
    lease_id: str
    principal_id: str
    principal_role: str


def principal_role_name(principal: Any) -> str:
    """Best (highest-rank) known role on *principal*, defaulting to viewer."""
    roles = getattr(principal, "roles", None) or frozenset()
    best = "viewer"
    best_rank = -1
    for role in roles:
        r = str(role).strip().lower()
        rank = _ROLE_RANK.get(r, -1)
        if rank > best_rank:
            best = r
            best_rank = rank
    return best


def principal_subject_id(principal: Any) -> str | None:
    """``subject_id`` string, or ``None`` when unauthenticated/missing."""
    subject = getattr(principal, "subject_id", None)
    return str(subject) if subject is not None else None


def check_vnc_relay_authz(
    *,
    principal: Any,
    hijack_session: Any | None,
    hijack_id: str,
) -> VncRelayAuth | str:
    """Return :class:`VncRelayAuth` on success, else a stable denial reason string.

    Pure helper for unit tests — no WebSocket / hub side effects.
    """
    if principal is None:
        return "authentication required"
    role = principal_role_name(principal)
    if _ROLE_RANK.get(role, -1) < _ROLE_RANK["operator"]:
        return "insufficient privileges"
    if hijack_session is None:
        return "invalid or expired hijack session"
    subject = principal_subject_id(principal)
    acquired_by = getattr(hijack_session, "acquired_by", None)
    if acquired_by is not None and subject != acquired_by:
        return "hijack lease not owned by caller"
    return VncRelayAuth(
        session_id=str(getattr(hijack_session, "hijack_id", hijack_id) or hijack_id),
        lease_id=str(getattr(hijack_session, "hijack_id", hijack_id) or hijack_id),
        principal_id=subject or "",
        principal_role=role,
    )


def _policy_can_inject(sid: str, lid: str, _pid: str, role: str) -> bool:
    """Adapt :func:`can_inject` (``None`` = allow) to the RFB filter bool callback."""
    return can_inject(sid, lid, role) is None


def _close_quietly(obj: Any) -> None:
    close = getattr(obj, "close", None)
    if callable(close):  # pragma: no branch - close is always callable on real streams
        with suppress(OSError):
            close()


def _shutdown_quietly(sock: socket.socket, how: int = socket.SHUT_RDWR) -> None:
    with suppress(OSError):
        sock.shutdown(how)


async def _close_ws(websocket: WebSocket, *, code: int, reason: str) -> None:
    with suppress(Exception):
        await websocket.close(code=code, reason=reason[:120])


def register_gui_vnc_ws_routes(
    hub: TermHub,
    router: APIRouter,
    *,
    upstream_factory: UpstreamDuplexFactory | None = None,
) -> None:
    """Attach the human VNC WebSocket relay to *router*.

    *upstream_factory* may also be set later as ``hub.vnc_upstream_factory``
    (callable with the same signature). When neither is available the route
    still authenticates then closes with 1013.
    """

    @router.websocket("/worker/{worker_id}/hijack/{hijack_id}/gui/vnc")
    async def ws_gui_vnc(
        websocket: WebSocket,
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(pattern=r"^[0-9a-f\-]{1,64}$"),
        target_id: str | None = Query(default=None),
    ) -> None:
        # Accept first so close codes reach the client (same pattern as worker WS auth).
        await websocket.accept()

        principal = getattr(getattr(websocket, "state", None), "uterm_principal", None)
        hs = await hub.get_rest_session(worker_id, hijack_id)
        auth = check_vnc_relay_authz(principal=principal, hijack_session=hs, hijack_id=hijack_id)
        if isinstance(auth, str):
            logger.info(
                "gui_vnc_denied worker_id=%s hijack_id=%s reason=%s",
                worker_id,
                hijack_id,
                auth,
            )
            await _close_ws(websocket, code=_CLOSE_POLICY, reason=auth)
            return

        factory = upstream_factory
        if factory is None:
            factory = getattr(hub, "vnc_upstream_factory", None)
        streams: tuple[BinaryIO, BinaryIO] | None = None
        if callable(factory):
            try:
                streams = factory(worker_id, target_id)
            except Exception as exc:
                logger.warning(
                    "gui_vnc_upstream_factory_error worker_id=%s target_id=%s error=%s",
                    worker_id,
                    target_id,
                    exc,
                )
                streams = None

        if streams is None:
            logger.info(
                "gui_vnc_upstream_unavailable worker_id=%s hijack_id=%s target_id=%s",
                worker_id,
                hijack_id,
                target_id,
            )
            await _close_ws(websocket, code=_CLOSE_UPSTREAM, reason="upstream unavailable")
            return

        upstream_r, upstream_w = streams
        await _run_ws_relay(
            websocket,
            upstream_r=upstream_r,
            upstream_w=upstream_w,
            auth=auth,
            worker_id=worker_id,
        )


async def _run_ws_relay(
    websocket: WebSocket,
    *,
    upstream_r: BinaryIO,
    upstream_w: BinaryIO,
    auth: VncRelayAuth,
    worker_id: str,
) -> None:
    """Bridge *websocket* ↔ upstream via :func:`run_human_relay_streams`."""
    browser_sock, relay_sock = socket.socketpair()
    relay_r: BinaryIO | None = None
    relay_w: BinaryIO | None = None
    try:
        browser_sock.setblocking(False)
        # Unbuffered: RFB banners must cross the socketpair without waiting for
        # a full stdio buffer or peer EOF (live VNC leaves the TCP session open).
        # socket.makefile has no closefd=; sockets closed in finally below.
        relay_r = relay_sock.makefile("rb", buffering=0)
        relay_w = relay_sock.makefile("wb", buffering=0)

        relay_done = threading.Event()
        relay_error: list[BaseException] = []

        def _run_relay() -> None:
            try:
                br, bw = relay_r, relay_w
                if br is None or bw is None:  # pragma: no cover — set before thread start
                    return
                run_human_relay_streams(
                    br,
                    bw,
                    upstream_r,
                    upstream_w,
                    can_inject=_policy_can_inject,
                    session_id=auth.session_id,
                    lease_id=auth.lease_id,
                    principal_id=auth.principal_id,
                    principal_role=auth.principal_role,
                )
            except BaseException as exc:
                relay_error.append(exc)
            finally:
                relay_done.set()
                _shutdown_quietly(relay_sock)

        thr = threading.Thread(target=_run_relay, name="gui-vnc-relay", daemon=True)
        thr.start()

        loop = asyncio.get_running_loop()

        # The two byte-pump loops below run as gathered tasks. Their live-IO exit
        # paths (peer disconnect → the caught socket/WS errors; upstream EOF →
        # break) fire during real relay operation, but a normal test-client close
        # cancels the tasks (CancelledError, not the caught set), so these exact
        # lines are exercised end-to-end, not by the unit harness. Excluded from
        # the line gate rather than faked.
        async def _ws_to_relay() -> None:
            try:
                while True:
                    message = await websocket.receive()
                    if message.get("type") == "websocket.disconnect":
                        break
                    data = message.get("bytes")
                    if data is None:
                        text = message.get("text")
                        if text is not None:  # pragma: no branch - frames carry bytes or text
                            data = text.encode("latin-1", errors="replace")
                    if not data:
                        continue
                    await loop.sock_sendall(browser_sock, data)
            except (WebSocketDisconnect, ConnectionError, OSError):  # pragma: no cover - live relay exit
                return
            finally:
                _shutdown_quietly(browser_sock, socket.SHUT_WR)

        async def _relay_to_ws() -> None:
            try:
                while True:
                    chunk = await loop.sock_recv(browser_sock, 65_536)
                    if not chunk:  # pragma: no cover - live relay exit (upstream EOF)
                        break
                    await websocket.send_bytes(chunk)
            except (WebSocketDisconnect, ConnectionError, OSError):  # pragma: no cover - live relay exit
                return

        try:
            await asyncio.gather(_ws_to_relay(), _relay_to_ws())
        finally:
            _shutdown_quietly(browser_sock)
            relay_done.wait(timeout=5.0)
            thr.join(timeout=2.0)
            if relay_error:
                logger.warning("gui_vnc_relay_error worker_id=%s error=%s", worker_id, relay_error[0])
            await _close_ws(websocket, code=_CLOSE_NORMAL, reason="eof")
    finally:
        _close_quietly(browser_sock)
        _close_quietly(relay_sock)
        _close_quietly(relay_r)
        _close_quietly(relay_w)
        _close_quietly(upstream_r)
        _close_quietly(upstream_w)
