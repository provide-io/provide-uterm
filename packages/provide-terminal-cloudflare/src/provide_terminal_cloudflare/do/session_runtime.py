from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from provide_terminal_cloudflare.api.http_routes import route_http
    from provide_terminal_cloudflare.api.ws_routes import handle_socket_message
    from provide_terminal_cloudflare.auth.jwt import JwtValidationError, decode_jwt, extract_bearer_or_cookie
    from provide_terminal_cloudflare.auth.jwt import resolve_role as _resolve_jwt_role
    from provide_terminal_cloudflare.bridge.hijack import HijackCoordinator, HijackSession
    from provide_terminal_cloudflare.cf_types import CFWebSocket, DurableObject, Response
    from provide_terminal_cloudflare.config import CloudflareConfig
    from provide_terminal_cloudflare.do._session_runtime_io import _SessionRuntimeIoMixin
    from provide_terminal_cloudflare.do.ushell import init_ushell, on_browser_connected
    from provide_terminal_cloudflare.do.ws_helpers import _WsHelperMixin
    from provide_terminal_cloudflare.state.registry import update_kv_session
    from provide_terminal_cloudflare.state.store import SqliteStateStore
except Exception:
    from api.http_routes import route_http  # type: ignore[import-not-found]
    from api.ws_routes import handle_socket_message  # type: ignore[import-not-found]
    from auth.jwt import JwtValidationError, decode_jwt, extract_bearer_or_cookie  # type: ignore[import-not-found]
    from auth.jwt import resolve_role as _resolve_jwt_role  # type: ignore[import-not-found]
    from bridge.hijack import HijackCoordinator, HijackSession  # type: ignore[import-not-found]
    from cf_types import CFWebSocket, DurableObject, Response  # type: ignore[import-not-found]
    from config import CloudflareConfig  # type: ignore[import-not-found]
    from do._session_runtime_io import _SessionRuntimeIoMixin  # type: ignore[import-not-found]
    from do.ushell import init_ushell, on_browser_connected  # type: ignore[import-not-found]
    from do.ws_helpers import _WsHelperMixin  # type: ignore[import-not-found]
    from state.registry import update_kv_session  # type: ignore[import-not-found]
    from state.store import SqliteStateStore  # type: ignore[import-not-found]

from provide.terminal.control_channel import encode_control

logger = logging.getLogger(__name__)


class SessionRuntime(_SessionRuntimeIoMixin, _WsHelperMixin, DurableObject):
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
        self.worker_ws: CFWebSocket | None = None
        self.browser_sockets: dict[str, CFWebSocket] = {}
        self.raw_sockets: dict[str, CFWebSocket] = {}
        self.browser_hijack_owner: dict[str, str] = {}
        self.last_snapshot: dict[str, Any] | None = None
        self.last_analysis: str | None = None
        self.input_mode: str = "hijack"

        # ushell — set for sessions whose worker_id starts with "ushell-".
        self._ushell: Any = None  # UshellConnector | None
        self._ushell_started: bool = False

        self._restore_state()

    def _derive_worker_id(self) -> str:
        name = getattr(getattr(self.ctx, "id", object()), "name", None)
        if callable(name):
            try:
                return str(name())
            except Exception as exc:
                logger.debug("failed to derive worker_id from durable object name: %s", exc)
        return "default"  # fallback: hex-ID addressed DO, not name-addressed

    def _restore_state(self) -> None:
        # Alarm registrations persist across cold starts — no need to re-arm here.
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
        stored_mode = row.get("input_mode")
        if stored_mode in {"hijack", "open"}:
            self.input_mode = stored_mode

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _extract_token(self, request: object) -> str | None:
        """Extract a token from Authorization header, CF_Authorization cookie, or query params."""
        token = extract_bearer_or_cookie(request)
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

    async def _resolve_principal(self, request: object) -> tuple[Any, Response | None]:
        """Validate JWT auth.

        Returns ``(principal, None)`` when auth succeeds or is not required
        (``none``/``dev`` mode), or ``(None, error_response)`` on failure.
        """
        if self.config.jwt.mode in {"none", "dev"}:
            return None, None
        # CF Access Service Auth: if the request carries a service token
        # header, CF Access already validated it — trust the request.
        try:
            cf_client_id = str(request.headers.get("CF-Access-Client-Id") or "")  # type: ignore[union-attr]
            if len(cf_client_id) > 0:
                return None, None
        except Exception:  # noqa: S110
            pass
        token = self._extract_token(request)
        if not token:
            return None, Response(
                json.dumps({"error": "authentication required"}, ensure_ascii=True),
                status=401,
                headers={"content-type": "application/json"},
            )
        try:
            principal = await decode_jwt(token, self.config.jwt)
            return principal, None
        except JwtValidationError as exc:
            return None, Response(
                json.dumps({"error": "invalid token", "detail": str(exc)}, ensure_ascii=True),
                status=401,
                headers={"content-type": "application/json"},
            )

    async def browser_role_for_request(self, request: object) -> str:
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
            principal = await decode_jwt(token, self.config.jwt)
            return _resolve_jwt_role(principal)
        except JwtValidationError:
            return "viewer"
        # Other exceptions (e.g. network errors fetching JWKS) propagate so the
        # caller returns a 5xx rather than silently downgrading the caller to viewer.

    # ------------------------------------------------------------------
    # Fetch / WS lifecycle
    # ------------------------------------------------------------------

    def _lazy_init_worker_id(self, request: object) -> None:
        """Update worker_id from the request URL when ctx.id.name() returned 'default'.

        Called at the start of fetch() so KV writes and state operations use
        the real worker_id extracted from the URL path.
        """
        if self.worker_id != "default":
            return
        try:
            path = urlparse(str(request.url)).path  # type: ignore[attr-defined]
        except Exception:
            return
        for prefix in ("/ws/worker/", "/ws/browser/", "/ws/raw/", "/worker/", "/api/sessions/"):
            if path.startswith(prefix):
                segment = path[len(prefix) :].split("/")[0]
                if segment:
                    self.worker_id = segment
                    return

    async def fetch(self, request: object) -> Response:
        # Resolve worker_id from URL when ctx.id.name() is unavailable (CF Python runtime bug).
        self._lazy_init_worker_id(request)

        # Parse URL once — reused for worker WS check and socket role routing.
        upgrade_header = str(request.headers.get("Upgrade") or "").lower()  # type: ignore[attr-defined]
        path = urlparse(str(request.url)).path  # type: ignore[attr-defined]

        # Worker WS connections authenticate with a bearer token, not JWT.
        # When worker_bearer_token is None (dev/none mode), this block is
        # skipped entirely and the request falls through to _resolve_principal()
        # which permits all callers in those modes.  In JWT mode, from_env()
        # guarantees worker_bearer_token is set (ValueError otherwise).
        _is_worker_ws = upgrade_header == "websocket" and path.startswith("/ws/worker/")
        if _is_worker_ws and self.config.worker_bearer_token:
            # CF Access service tokens bypass worker bearer token check.
            _cf_client = str(request.headers.get("CF-Access-Client-Id") or "")  # type: ignore[union-attr]
            if _cf_client.endswith(".access"):
                principal, auth_error = None, None
            else:
                token = extract_bearer_or_cookie(request)
                if not token or not secrets.compare_digest(token, self.config.worker_bearer_token):
                    return Response(
                        json.dumps({"error": "worker authentication required"}),
                        status=403,
                        headers={"content-type": "application/json"},
                    )
                principal, auth_error = None, None
        else:
            principal, auth_error = await self._resolve_principal(request)
            if auth_error is not None:
                return auth_error
        if upgrade_header == "websocket":
            from js import WebSocketPair  # type: ignore[import-not-found]

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
                # Encode socket type, browser role, and worker_id for hibernation safety.
                # Format: "browser:admin:e2e-abc123", "worker:admin:e2e-abc123", "raw:admin:e2e-abc123"
                # worker_id in the attachment lets webSocketClose recover the ID after hibernation.
                server.serializeAttachment(f"{socket_role}:{browser_role}:{self.worker_id}")
            except Exception as exc:
                logger.warning(
                    "serializeAttachment failed — role lost on hibernation worker_id=%s: %s",
                    self.worker_id,
                    exc,
                )
                server._ut_role = socket_role
                server._ut_browser_role = browser_role
            # Register here so the role is available if fetch() is re-entered
            # before webSocketOpen() fires (hibernation-restore path).
            self._register_socket(server, socket_role)

            # For worker connections, write KV registration eagerly in fetch() before
            # returning 101. In CF hibernation mode, async operations in webSocketOpen()
            # may not complete if the DO hibernates before the handler finishes.
            if socket_role == "worker":
                try:
                    await update_kv_session(
                        self.env,
                        self.worker_id,
                        connected=True,
                        hijacked=self.hijack.session is not None,
                        input_mode=self.input_mode,
                    )
                except Exception as exc:
                    logger.warning("kv register worker in fetch() failed: %s", exc)

            # Send hello in fetch() before 101 — webSocketOpen() may be dropped after hibernation.
            if socket_role == "browser":
                # Initialize ushell connector if this is an ushell-* session.
                init_ushell(self)
                # Issue a resume token for this browser session
                resume_token = secrets.token_urlsafe(32)
                resume_ttl_s = float(getattr(self.config, "resume_ttl_s", 300))
                self.store.create_resume_token(resume_token, self.worker_id, browser_role, resume_ttl_s)
                try:
                    server.send(
                        encode_control(
                            {
                                "type": "hello",
                                "worker_id": self.worker_id,
                                "worker_online": self.worker_ws is not None or self._ushell is not None,
                                "can_hijack": browser_role == "admin",
                                "input_mode": self.input_mode,
                                "role": browser_role,
                                "hijack_control": "rest",
                                "hijack_step_supported": True,
                                "resume_supported": True,
                                "resume_token": resume_token,
                                "ts": time.time(),
                            }
                        )
                    )
                except Exception as exc:
                    logger.warning("failed to send hello from fetch(): %s", exc)

            return Response(None, status=101, web_socket=client)
        return await route_http(self, request)

    async def webSocketOpen(self, ws: CFWebSocket) -> None:  # noqa: N802
        ws_id = self.ws_key(ws)
        role = self._socket_role(ws)
        self._register_socket(ws, role)
        if role == "worker":
            self.worker_ws = ws
            await self.broadcast_worker_frame(
                {"type": "worker_connected", "worker_id": self.worker_id, "ts": time.time()}
            )
            await update_kv_session(
                self.env,
                self.worker_id,
                connected=True,
                hijacked=self.hijack.session is not None,
                input_mode=self.input_mode,
            )
        elif role == "raw":
            self.raw_sockets[ws_id] = ws
            if self.last_snapshot is not None and isinstance(self.last_snapshot.get("screen"), str):
                await self._send_text(ws, str(self.last_snapshot.get("screen")))
        else:
            self.browser_sockets[ws_id] = ws
            browser_role = self._socket_browser_role(ws)
            # Issue a resume token for this browser session
            _open_resume_token = secrets.token_urlsafe(32)
            _open_resume_ttl = float(getattr(self.config, "resume_ttl_s", 300))
            self.store.create_resume_token(_open_resume_token, self.worker_id, browser_role, _open_resume_ttl)
            await self.send_ws(
                ws,
                {
                    "type": "hello",
                    "worker_id": self.worker_id,
                    "worker_online": self.worker_ws is not None or self._ushell is not None,
                    # can_hijack and role reflect the JWT-resolved browser role.
                    "can_hijack": browser_role == "admin",
                    "input_mode": self.input_mode,
                    "role": browser_role,
                    "hijack_control": "rest",
                    "hijack_step_supported": True,
                    "resume_supported": True,
                    "resume_token": _open_resume_token,
                    "ts": time.time(),
                },
            )
            await self.send_hijack_state(ws)
            if self.last_snapshot is not None:
                await self.send_ws(ws, self.last_snapshot)
            # For ushell sessions, broadcast worker_connected + welcome on first browser join.
            await on_browser_connected(self)

    async def webSocketMessage(self, ws: CFWebSocket, message: Any) -> None:  # noqa: N802
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

    async def webSocketClose(self, ws: CFWebSocket, code: int, reason: str, was_clean: bool = True) -> None:  # noqa: N802
        _ = (code, reason, was_clean)
        # Use _socket_role() instead of `ws is self.worker_ws` — after hibernation,
        # self.worker_ws is None so the identity check would always be False.
        role = self._socket_role(ws)
        wid = self._socket_worker_id(ws)
        self._remove_ws(ws)
        if role == "worker":
            await self.broadcast_worker_frame({"type": "worker_disconnected", "worker_id": wid, "ts": time.time()})
            await update_kv_session(self.env, wid, connected=False)

    async def webSocketError(self, ws: CFWebSocket, error: Any) -> None:  # noqa: N802
        role = self._socket_role(ws)
        wid = self._socket_worker_id(ws)
        logger.warning("ws_error worker_id=%s role=%s error=%s", wid, role, error)
        self._remove_ws(ws)
        if role == "worker":
            await self.broadcast_worker_frame({"type": "worker_disconnected", "worker_id": wid, "ts": time.time()})
            await update_kv_session(self.env, wid, connected=False)
