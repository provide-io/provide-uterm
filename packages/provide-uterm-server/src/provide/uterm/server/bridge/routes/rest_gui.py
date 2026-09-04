#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""GUI (graphical console) REST routes for the hijack hub.

Server-side handlers for the ``/gui/`` surface the Python client
(:class:`provide.uterm.client.hijack.HijackClient`) + the ``uterm-mcp`` GUI
tools already call. Path-compatible with the C# canonical
(``packages/provide-uterm-csharp/.../Server/UtermServer.Gui.cs``) and the Go
port (``packages/provide-uterm-go/server/bridge_rest.go``).

Registers:
- ``POST /worker/{id}/gui/attach``                       — resolve a graphical target + attach a session
- ``GET  /worker/{id}/hijack/{hid}/gui/screenshot``      — base64 PNG of the console
- ``POST /worker/{id}/hijack/{hid}/gui/click``           — inject a pointer click
- ``POST /worker/{id}/hijack/{hid}/gui/type``            — inject typed text
- ``POST /worker/{id}/hijack/{hid}/gui/key``             — inject a named key
- ``POST /worker/{id}/hijack/{hid}/gui/drag``            — inject a pointer drag

.. rubric:: Authorisation

Like the sibling hijack routes, these carry **no built-in per-request authz**.
Session-level gating (``session.read`` for screenshot, ``session.control.hijack``
for attach + the input routes) is applied by the
:func:`~provide.uterm.server.app.hub_authz.build_require_hub_route_authz`
dependency that protects the hub router in the full app. The attach handler
additionally enforces the ``graphical.session.attach`` capability + the
principal's tenant scope against the graphical-target registry.
"""

from __future__ import annotations

import base64
import time
from typing import TYPE_CHECKING, Any, cast

try:
    from fastapi import APIRouter, Path, Request
    from fastapi.responses import JSONResponse
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for hijack routes: pip install 'provide-uterm[websocket]'") from _e

from provide.telemetry import get_logger
from provide.uterm.server.bridge.models import WorkerTermState
from provide.uterm.server.graphical_targets import PROTOCOL_MEMORY, scope_for_tenant
from provide.uterm.server.gui_session import GraphicalSession, MemoryGraphicalSession, encode_rgba_png
from provide.uterm.server.vnc_upstream import dial_config_from_target

if TYPE_CHECKING:
    from provide.uterm.server.authorization import AuthorizationService
    from provide.uterm.server.bridge.hub import TermHub
    from provide.uterm.server.graphical_targets import InMemoryGraphicalTargetRegistry

logger = get_logger(__name__)

CAP_ATTACH = "graphical.session.attach"

# key_name → X11 keysym (matches the C#/Go canonical switch).
_KEY_SYMS: dict[str, int] = {
    "Enter": 0xFF0D,
    "Tab": 0xFF09,
    "Esc": 0xFF1B,
    "Backspace": 0xFF08,
    "Up": 0xFF52,
    "Down": 0xFF54,
    "Left": 0xFF51,
    "Right": 0xFF53,
}

# button name → RFB button bitmask.
_BUTTON_MASKS: dict[str, int] = {"left": 1, "middle": 2, "right": 4}


def _mono_to_wall(mono_ts: float) -> float:
    """Convert a monotonic timestamp to wall-clock for external API responses."""
    return time.time() + (mono_ts - time.monotonic())


async def _read_json_object(request: Request) -> dict[str, Any]:
    """Read a JSON object body; an invalid/absent/non-object body yields ``{}``."""
    try:
        raw = await request.json()
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _int_field(body: dict[str, Any], key: str, default: int = 0) -> int:
    """Read an integer body field, tolerating strings; else ``default``."""
    raw = body.get(key, default)
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return default
    return default


def _str_field(body: dict[str, Any], key: str, default: str = "") -> str:
    """Read a string body field; else ``default``."""
    raw = body.get(key, default)
    return raw if isinstance(raw, str) else default


def _block_private(request: Request) -> bool:
    """Whether private/loopback RFB endpoints are refused for this deployment.

    Default False: reaching an internal console is the product's purpose, and a
    hosted deployment opts in. Cloud-metadata addresses are blocked by the guard
    either way, so the default cannot expose credentials.
    """
    config = getattr(request.app.state, "uterm_config", None)
    security = getattr(config, "security", None)
    return bool(getattr(security, "block_private_connector_targets", False))


def register_gui_routes(hub: TermHub, router: APIRouter) -> None:
    """Attach the ``/gui/`` REST routes to *router*.

    .. warning::
        No authentication is applied here — see the module docstring. Mount the
        router behind the hub authz dependency before exposing it.
    """

    @router.post("/worker/{worker_id}/gui/attach")
    async def gui_attach(
        request: Request,
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
    ) -> Any:
        principal = request.state.uterm_principal
        authz = cast("AuthorizationService", request.app.state.uterm_authz)
        if not await authz.has_capability(principal, CAP_ATTACH):
            return JSONResponse({"error": "insufficient privileges"}, status_code=403)

        body = await _read_json_object(request)
        target_id = _str_field(body, "target_id").strip()
        if not target_id:
            return JSONResponse({"error": "target_id is required for gui attach"}, status_code=422)

        scope, ok = scope_for_tenant(getattr(principal, "tenant_id", None) or "")
        if not ok or scope is None:
            return JSONResponse({"error": "graphical target access denied"}, status_code=403)

        registry = cast("InMemoryGraphicalTargetRegistry", request.app.state.uterm_graphical_targets)
        target = registry.get(scope, target_id)
        if target is None:
            return JSONResponse({"error": "target not found"}, status_code=404)

        protocol = target.protocol.strip().lower()
        session: GraphicalSession
        if protocol == PROTOCOL_MEMORY:
            session = MemoryGraphicalSession(max(1, target.width), max(1, target.height))
        # dial_config_from_target returns None for every non-rfb protocol, so the
        # walrus is the rfb test — and it narrows ``dial`` for the ``.host`` read
        # below. Asking ``protocol == PROTOCOL_RFB`` first and then re-checking
        # ``dial is None`` asks the same question twice and leaves the second
        # answer unreachable.
        elif (dial := dial_config_from_target(target)) is not None:
            from provide.uterm.server.egress import assert_connector_target_allowed
            from provide.uterm.server.rfb_session import RfbGraphicalSession

            try:
                # Connector-grade egress, matching the C# canonical
                # (UtermServer.Gui.cs): cloud-metadata is always blocked, and
                # private/loopback follows block_private_connector_targets so an
                # internal console stays reachable when the flag is off. Without
                # this a tenant who can name a target can name 169.254.169.254.
                await assert_connector_target_allowed(dial.host, block_private=_block_private(request))
            except Exception as exc:
                logger.info("gui_attach_egress_blocked target=%s host=%s", target.target_id, dial.host)
                return JSONResponse({"error": f"invalid endpoint: {exc}"}, status_code=403)

            try:
                session = RfbGraphicalSession.connect(target)
            except (ConnectionError, OSError) as exc:
                # The registry entry is valid and the console is unreachable, so
                # this is a bad gateway rather than a bad request.
                logger.info("gui_attach_rfb_failed target=%s error=%s", target.target_id, exc)
                return JSONResponse({"error": f"rfb connect failed: {exc}"}, status_code=502)
        else:
            return JSONResponse({"error": f"graphical protocol not supported: {protocol}"}, status_code=501)

        st = hub.registry.get(worker_id)
        if st is None:
            st = WorkerTermState()
            hub.registry.put(worker_id, st)
        # Replacing a live session must not leak its socket and reader thread.
        previous = getattr(st, "graphical_session", None)
        closer = getattr(previous, "close", None)
        if callable(closer) and previous is not session:
            try:
                closer()
            except Exception:
                logger.debug("gui_attach_previous_close_failed", exc_info=True)
        st.graphical_session = session
        logger.info("gui_attach_ok worker_id=%s target_id=%s protocol=%s", worker_id, target_id, protocol)
        return {"ok": True, "target_id": target_id}

    @router.get("/worker/{worker_id}/hijack/{hijack_id}/gui/screenshot")
    async def gui_screenshot(
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(pattern=r"^[0-9a-f\-]{1,64}$"),
    ) -> Any:
        hs = await hub.get_rest_session(worker_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        gui = _graphical_session(hub, worker_id)
        if gui is None:
            return JSONResponse({"error": "No graphical session attached."}, status_code=404)
        img = gui.screenshot()
        png = encode_rgba_png(img.width, img.height, img.pixels)
        return {
            "ok": True,
            "worker_id": worker_id,
            "hijack_id": hijack_id,
            "screenshot": base64.b64encode(png).decode("ascii"),
            "lease_expires_at": _mono_to_wall(hs.lease_expires_at),
        }

    @router.post("/worker/{worker_id}/hijack/{hijack_id}/gui/click")
    async def gui_click(
        request: Request,
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(pattern=r"^[0-9a-f\-]{1,64}$"),
    ) -> Any:
        gui = await _require_graphical_session(hub, request, worker_id, hijack_id)
        if isinstance(gui, JSONResponse):
            return gui
        body = await _read_json_object(request)
        button = _str_field(body, "button", "left")
        if button not in _BUTTON_MASKS:
            return JSONResponse({"error": "invalid button: must be left, middle, or right"}, status_code=422)
        x = _int_field(body, "x")
        y = _int_field(body, "y")
        mask = _BUTTON_MASKS[button]
        gui.inject_pointer(x, y, mask)
        gui.inject_pointer(x, y, 0)
        return {"ok": True}

    @router.post("/worker/{worker_id}/hijack/{hijack_id}/gui/type")
    async def gui_type(
        request: Request,
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(pattern=r"^[0-9a-f\-]{1,64}$"),
    ) -> Any:
        gui = await _require_graphical_session(hub, request, worker_id, hijack_id)
        if isinstance(gui, JSONResponse):
            return gui
        body = await _read_json_object(request)
        for ch in _str_field(body, "text"):
            gui.inject_key(ord(ch), True)
            gui.inject_key(ord(ch), False)
        return {"ok": True}

    @router.post("/worker/{worker_id}/hijack/{hijack_id}/gui/key")
    async def gui_key(
        request: Request,
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(pattern=r"^[0-9a-f\-]{1,64}$"),
    ) -> Any:
        gui = await _require_graphical_session(hub, request, worker_id, hijack_id)
        if isinstance(gui, JSONResponse):
            return gui
        body = await _read_json_object(request)
        sym = _KEY_SYMS.get(_str_field(body, "key_name"), 0)
        gui.inject_key(sym, True)
        gui.inject_key(sym, False)
        return {"ok": True}

    @router.post("/worker/{worker_id}/hijack/{hijack_id}/gui/drag")
    async def gui_drag(
        request: Request,
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(pattern=r"^[0-9a-f\-]{1,64}$"),
    ) -> Any:
        gui = await _require_graphical_session(hub, request, worker_id, hijack_id)
        if isinstance(gui, JSONResponse):
            return gui
        body = await _read_json_object(request)
        start_x = _int_field(body, "start_x")
        start_y = _int_field(body, "start_y")
        end_x = _int_field(body, "end_x")
        end_y = _int_field(body, "end_y")
        gui.inject_pointer(start_x, start_y, 1)
        gui.inject_pointer(end_x, end_y, 1)
        gui.inject_pointer(end_x, end_y, 0)
        return {"ok": True}


def _graphical_session(hub: TermHub, worker_id: str) -> GraphicalSession | None:
    """Return the attached graphical session for *worker_id*, or ``None``."""
    st = hub.registry.get(worker_id)
    return st.graphical_session if st is not None else None


async def _require_graphical_session(
    hub: TermHub, request: Request, worker_id: str, hijack_id: str
) -> GraphicalSession | JSONResponse:
    """Resolve the graphical session behind an active hijack, else an error response.

    Inject is principal-bound: the caller must match ``acquired_by`` on the
    lease (not merely possess the hijack_id). Missing lease/session → 404;
    ownership mismatch → 403.
    """
    from provide.uterm.server.bridge.routes.rest import _principal_subject

    hs = await hub.get_rest_session(worker_id, hijack_id)
    if hs is None:
        return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
    requester = _principal_subject(request)
    if hs.acquired_by is not None and requester != hs.acquired_by:
        return JSONResponse({"error": "hijack lease not owned by caller"}, status_code=403)
    gui = _graphical_session(hub, worker_id)
    if gui is None:
        return JSONResponse({"error": "No graphical session attached."}, status_code=404)
    return gui
