from __future__ import annotations

import contextlib
import inspect
import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from provide_terminal_cloudflare.api.http_routes import route_http
    from provide_terminal_cloudflare.api.ws_routes import handle_socket_message
    from provide_terminal_cloudflare.auth.jwt import JwtValidationError, decode_jwt
    from provide_terminal_cloudflare.auth.jwt import resolve_role as _resolve_jwt_role
    from provide_terminal_cloudflare.bridge.hijack import HijackCoordinator, HijackSession
    from provide_terminal_cloudflare.cf_types import DurableObject, Response
    from provide_terminal_cloudflare.config import CloudflareConfig
    from provide_terminal_cloudflare.state.store import LeaseRecord, SqliteStateStore
except Exception:
    from api.http_routes import route_http
    from api.ws_routes import handle_socket_message
    from auth.jwt import JwtValidationError, decode_jwt
    from auth.jwt import resolve_role as _resolve_jwt_role
    from bridge.hijack import HijackCoordinator, HijackSession
    from cf_types import DurableObject, Response
    from config import CloudflareConfig
    from state.store import LeaseRecord, SqliteStateStore

logger = logging.getLogger(__name__)


class SessionRuntime(DurableObject):
    """Durable Object runtime for one worker/session channel."""

    def __init__(self, ctx: Any, env: Any):
        super().__init__(ctx, env)
        self.config = CloudflareConfig.from_env(env)
        sql_exec = getattr(getattr(ctx, "storage", object()), "sql", None)
        if sql_exec is None or not hasattr(sql_exec, "exec"):
            raise RuntimeError("Durable Object sqlite storage is required")
        self.store = SqliteStateStore(sql_exec.exec, max_events_per_worker=self.config.limits.max_events_per_worker)
        self.store.migrate()

        self.worker_id = self._derive_worker_id()
        self.hijack = HijackCoordinator()
        self.worker_ws: Any | None = None
        self.browser_sockets: dict[str, Any] = {}
        self.raw_sockets: dict[str, Any] = {}
        self.browser_hijack_owner: dict[str, str] = {}
        self.last_snapshot: dict[str, Any] | None = None

        self._restore_state()

    def _derive_worker_id(self) -> str:
        name = getattr(getattr(self.ctx, "id", object()), "name", None)
        if callable(name):
            try:
                return str(name())
            except Exception as exc:
                logger.debug("failed to derive worker_id from durable object name: %s", exc)
        # Fallback: ctx.id.name() unavailable (hex-ID addressed DO, not name-addressed).
        # In production all DOs are addressed via idFromName(worker_id), so this should
        # never occur. If it does, multiple unnamed instances would collide on the same
        # session row — investigate the routing configuration.
        return "default"

    def _ws_key(self, ws: Any) -> str:
        try:
            existing = getattr(ws, "_ut_ws_key", None)
            if isinstance(existing, str) and existing:
                return existing
        except Exception:
            existing = None

        key = f"{time.time_ns()}_{secrets.token_hex(4)}"
        with contextlib.suppress(Exception):
            ws._ut_ws_key = key
        return key

    def _restore_state(self) -> None:
        row = self.store.load_session(self.worker_id)
        if row is None:
            return
        hijack_id = row.get("hijack_id")
        owner = row.get("owner")
        lease_expires_at = row.get("lease_expires_at")
        if (
            isinstance(hijack_id, str)
            and isinstance(owner, str)
            and isinstance(lease_expires_at, (float, int))
            and float(lease_expires_at) > time.time()
        ):
            self.hijack._session = HijackSession(
                hijack_id=hijack_id,
                owner=owner,
                lease_expires_at=float(lease_expires_at),
            )
        snapshot = row.get("last_snapshot")
        if isinstance(snapshot, dict):
            self.last_snapshot = snapshot

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _extract_token(self, request: object) -> str | None:
        """Extract a token from Authorization header and optional query params."""
        try:
            auth_header = str(request.headers.get("Authorization") or "")  # type: ignore[attr-defined]
        except Exception:
            auth_header = ""
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
            if token:
                return token
        if not self.config.jwt.allow_query_token:
            return None
        try:
            qs = parse_qs(urlparse(str(request.url)).query)  # type: ignore[attr-defined]
            candidates = qs.get("token", []) + qs.get("access_token", [])
            if candidates:
                return candidates[0] or None
        except Exception as exc:
            logger.debug("failed to parse query token: %s", exc)
        return None

    def _resolve_principal(self, request: object) -> tuple[Any, Response | None]:
        """Validate JWT auth.

        Returns ``(principal, None)`` when auth succeeds or is not required
        (``none``/``dev`` mode), or ``(None, error_response)`` on failure.
        """
        if self.config.jwt.mode in {"none", "dev"}:
            return None, None
        token = self._extract_token(request)
        if not token:
            return None, Response(
                json.dumps({"error": "authentication required"}, ensure_ascii=True),
                status=401,
                headers={"content-type": "application/json"},
            )
        try:
            principal = decode_jwt(token, self.config.jwt)
            return principal, None
        except JwtValidationError as exc:
            return None, Response(
                json.dumps({"error": "invalid token", "detail": str(exc)}, ensure_ascii=True),
                status=401,
                headers={"content-type": "application/json"},
            )

    def browser_role_for_request(self, request: object) -> str:
        """Return the caller's role string based on JWT or auth mode.

        Returns ``"admin"`` in ``none``/``dev`` mode (open access). In ``jwt`` mode,
        decodes the token and returns ``"admin"``, ``"operator"``, or ``"viewer"``.
        Falls back to ``"viewer"`` if the token is missing or invalid (the token
        was already validated in ``fetch()``; this is only for role extraction).
        """
        if self.config.jwt.mode in {"none", "dev"}:
            return "admin"
        token = self._extract_token(request)
        if not token:
            return "viewer"
        try:
            principal = decode_jwt(token, self.config.jwt)
            return _resolve_jwt_role(principal)
        except Exception:
            return "viewer"

    # ------------------------------------------------------------------
    # WebSocket role helpers
    # ------------------------------------------------------------------

    def _socket_role(self, ws: Any) -> str:
        """Return the socket type: ``"browser"``, ``"worker"``, or ``"raw"``."""
        try:
            attachment = ws.deserializeAttachment()
            if isinstance(attachment, str):
                if attachment in {"browser", "worker", "raw"}:
                    return attachment  # legacy plain-string format
                # New format: "type:browser_role" e.g. "browser:admin"
                parts = attachment.split(":", 1)
                if parts[0] in {"browser", "worker", "raw"}:
                    return parts[0]
            role = None
            if hasattr(attachment, "get"):
                role = attachment.get("role")
            if role is None and hasattr(attachment, "role"):
                role = attachment.role
            if role is None and hasattr(attachment, "to_py"):
                try:
                    py_attachment = attachment.to_py()
                    if isinstance(py_attachment, str):
                        role = py_attachment
                    elif isinstance(py_attachment, dict):
                        role = py_attachment.get("role")
                except Exception:
                    role = None
            if isinstance(role, str) and role in {"browser", "worker", "raw"}:
                return role
        except Exception:
            role = None
        if role is None:
            candidate = getattr(ws, "_ut_role", None)
            if isinstance(candidate, str):
                return candidate
        return "browser"

    def _socket_browser_role(self, ws: Any) -> str:
        """Return the JWT-resolved browser role from the socket attachment.

        Defaults to ``"admin"`` when auth mode is ``none``/``dev`` or when the
        attachment does not carry a role (legacy connections).
        """
        try:
            attachment = ws.deserializeAttachment()
            if isinstance(attachment, str):
                parts = attachment.split(":", 1)
                if len(parts) == 2 and parts[1] in {"admin", "operator", "viewer"}:
                    return parts[1]
        except Exception as exc:
            logger.debug("failed to deserialize browser role attachment: %s", exc)
        # Instance-attribute fallback (set when serializeAttachment raises).
        role = getattr(ws, "_ut_browser_role", None)
        if isinstance(role, str) and role in {"admin", "operator", "viewer"}:
            return role
        return "admin"

    def _register_socket(self, ws: Any, role: str) -> None:
        ws_id = self._ws_key(ws)
        if role == "worker":
            self.worker_ws = ws
            return
        if role == "raw":
            self.raw_sockets[ws_id] = ws
            return
        self.browser_sockets[ws_id] = ws

    # ------------------------------------------------------------------
    # Fetch / WS lifecycle
    # ------------------------------------------------------------------

    async def fetch(self, request: object) -> Response:
        # Validate JWT before processing any request.
        principal, auth_error = self._resolve_principal(request)
        if auth_error is not None:
            return auth_error

        upgrade_header = str(request.headers.get("Upgrade") or "").lower()  # type: ignore[attr-defined]
        if upgrade_header == "websocket":
            from js import WebSocketPair

            path = urlparse(str(request.url)).path  # type: ignore[attr-defined]
            socket_role = "browser"
            if path.startswith("/ws/worker/"):
                socket_role = "worker"
            elif path.startswith("/ws/raw/"):
                socket_role = "raw"

            # Resolve browser role from JWT (defaults to "admin" in dev/none mode).
            browser_role = "admin" if principal is None else _resolve_jwt_role(principal)

            client, server = WebSocketPair.new().object_values()
            self.ctx.acceptWebSocket(server)
            try:
                # Encode socket type and browser role together for hibernation safety.
                # Format: "browser:admin", "worker:", "raw:"
                server.serializeAttachment(f"{socket_role}:{browser_role}")
            except Exception:
                server._ut_role = socket_role
                server._ut_browser_role = browser_role
            # Register here so the role is available if fetch() is re-entered
            # before webSocketOpen() fires (hibernation-restore path).
            self._register_socket(server, socket_role)
            return Response(None, status=101, web_socket=client)
        return await route_http(self, request)

    async def webSocketOpen(self, ws: Any) -> None:  # noqa: N802
        ws_id = self._ws_key(ws)
        role = self._socket_role(ws)
        self._register_socket(ws, role)
        if role == "worker":
            self.worker_ws = ws
            await self.broadcast_worker_frame(
                {"type": "worker_connected", "worker_id": self.worker_id, "ts": time.time()}
            )
        elif role == "raw":
            self.raw_sockets[ws_id] = ws
            if self.last_snapshot is not None and isinstance(self.last_snapshot.get("screen"), str):
                await self._send_text(ws, str(self.last_snapshot.get("screen")))
        else:
            self.browser_sockets[ws_id] = ws
            browser_role = self._socket_browser_role(ws)
            await self.send_ws(
                ws,
                {
                    "type": "hello",
                    "worker_id": self.worker_id,
                    "worker_online": self.worker_ws is not None,
                    # can_hijack and role reflect the JWT-resolved browser role.
                    "can_hijack": browser_role == "admin",
                    "input_mode": "hijack",
                    "role": browser_role,
                    "hijack_control": "rest",
                    "hijack_step_supported": True,
                    "capabilities": {
                        "hijack_control": "rest",
                        "hijack_step_supported": True,
                    },
                    "ts": time.time(),
                },
            )
            await self.send_hijack_state(ws)
            if self.last_snapshot is not None:
                await self.send_ws(ws, self.last_snapshot)

    async def webSocketMessage(self, ws: Any, message: Any) -> None:  # noqa: N802
        role = self._socket_role(ws)
        self._register_socket(ws, role)
        if role == "raw":
            payload = (
                message.decode("latin-1", errors="replace") if isinstance(message, (bytes, bytearray)) else str(message)
            )
            await self.push_worker_input(payload)
            return

        raw = message if isinstance(message, str) else str(message)
        await handle_socket_message(self, ws, raw, is_worker=(role == "worker"))

    def _remove_ws(self, ws: Any) -> None:
        """Remove *ws* from all socket registries (worker, browser, raw)."""
        ws_id = self._ws_key(ws)
        if ws is self.worker_ws:
            self.worker_ws = None
        self.browser_sockets.pop(ws_id, None)
        self.raw_sockets.pop(ws_id, None)
        self.browser_hijack_owner.pop(ws_id, None)

    async def webSocketClose(self, ws: Any, code: int, reason: str, was_clean: bool = True) -> None:  # noqa: N802
        _ = (code, reason, was_clean)
        was_worker = ws is self.worker_ws
        self._remove_ws(ws)
        if was_worker:
            await self.broadcast_worker_frame(
                {"type": "worker_disconnected", "worker_id": self.worker_id, "ts": time.time()}
            )

    async def webSocketError(self, ws: Any, error: Any) -> None:  # noqa: N802
        """Handle a network-level error on a hibernated socket.

        Called by the Cloudflare DO runtime when a socket experiences an error
        during hibernation. Cleans up the socket from all registries so stale
        handles don't block future connections.
        """
        logger.warning("ws_error worker_id=%s error=%s", self.worker_id, error)
        was_worker = ws is self.worker_ws
        self._remove_ws(ws)
        if was_worker:
            await self.broadcast_worker_frame(
                {"type": "worker_disconnected", "worker_id": self.worker_id, "ts": time.time()}
            )

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    async def request_json(self, request: object) -> dict[str, Any]:
        body = await request.text()  # type: ignore[attr-defined]
        if not body:
            return {}
        value = json.loads(body)
        if not isinstance(value, dict):
            return {}
        return value

    def persist_lease(self, session: HijackSession | None) -> None:
        if session is None:
            return
        self.store.save_lease(
            LeaseRecord(
                worker_id=self.worker_id,
                hijack_id=session.hijack_id,
                owner=session.owner,
                lease_expires_at=session.lease_expires_at,
            )
        )

    def clear_lease(self) -> None:
        self.store.clear_lease(self.worker_id)

    # ------------------------------------------------------------------
    # Send helpers
    # ------------------------------------------------------------------

    async def send_ws(self, ws: Any, payload: dict[str, Any]) -> None:
        await self._send_text(ws, json.dumps(payload, ensure_ascii=True))

    async def _send_text(self, ws: Any, payload: str) -> None:
        result = ws.send(payload)
        if inspect.isawaitable(result):
            await result

    async def send_hijack_state(self, ws: Any) -> None:
        ws_id = self._ws_key(ws)
        session = self.hijack.session
        owner = None
        if session is not None:
            owner = "me" if self.browser_hijack_owner.get(ws_id) == session.hijack_id else "other"
        await self.send_ws(
            ws,
            {
                "type": "hijack_state",
                "hijacked": session is not None,
                "owner": owner,
                "lease_expires_at": (session.lease_expires_at if session is not None else None),
                "ts": time.time(),
            },
        )

    async def broadcast_hijack_state(self) -> None:
        for ws_id, ws in list(self.browser_sockets.items()):
            try:
                await self.send_hijack_state(ws)
            except Exception:
                self.browser_sockets.pop(ws_id, None)
                self.browser_hijack_owner.pop(ws_id, None)

    # ------------------------------------------------------------------
    # Worker I/O
    # ------------------------------------------------------------------

    async def push_worker_control(self, action: str, *, owner: str, lease_s: int) -> bool:
        if self.worker_ws is None:
            return False
        await self.send_ws(
            self.worker_ws,
            {"type": "control", "action": action, "owner": owner, "lease_s": lease_s, "ts": time.time()},
        )
        return True

    async def push_worker_input(self, data: str) -> bool:
        if self.worker_ws is None:
            return False
        await self.send_ws(self.worker_ws, {"type": "input", "data": data, "ts": time.time()})
        return True

    async def broadcast_to_browsers(self, payload: dict[str, Any]) -> None:
        for ws_id, ws in list(self.browser_sockets.items()):
            try:
                await self.send_ws(ws, payload)
            except Exception:
                self.browser_sockets.pop(ws_id, None)
                self.browser_hijack_owner.pop(ws_id, None)

    async def broadcast_worker_frame(self, payload: dict[str, Any]) -> None:
        self.store.append_event(self.worker_id, str(payload.get("type") or "event"), payload)
        await self.broadcast_to_browsers(payload)

        text_payload: str | None = None
        frame_type = str(payload.get("type") or "")
        if frame_type == "term":
            text_payload = str(payload.get("data") or "")
        elif frame_type == "snapshot":
            screen = payload.get("screen")
            text_payload = str(screen) if screen is not None else ""
        elif frame_type == "worker_connected":
            text_payload = "\r\n[worker connected]\r\n"
        elif frame_type == "worker_disconnected":
            text_payload = "\r\n[worker disconnected]\r\n"

        if text_payload is None:
            return

        for ws_id, ws in list(self.raw_sockets.items()):
            try:
                await self._send_text(ws, text_payload)
            except Exception:
                self.raw_sockets.pop(ws_id, None)
