from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from provide.terminal.bridge.contracts import CURRENT_PROTOCOL_VERSION

try:
    from provide.terminal.cloudflare.api.http_routes import route_http
    from provide.terminal.cloudflare.api.ws_routes import handle_socket_message
    from provide.terminal.cloudflare.auth.jwt import JwtValidationError, decode_jwt, extract_bearer_or_cookie
    from provide.terminal.cloudflare.auth.jwt import resolve_role as _resolve_jwt_role
    from provide.terminal.cloudflare.bridge.hijack import HijackCoordinator
    from provide.terminal.cloudflare.cf_types import CFWebSocket, DurableObject, Response
    from provide.terminal.cloudflare.config import CloudflareConfig
    from provide.terminal.cloudflare.do._session_runtime_io import _SessionRuntimeIoMixin
    from provide.terminal.cloudflare.do.ushell import init_ushell, on_browser_connected
    from provide.terminal.cloudflare.do.ws_helpers import _WsHelperMixin
    from provide.terminal.cloudflare.state.registry import update_kv_session
    from provide.terminal.cloudflare.state.store import SqliteStateStore
except Exception:
    from api.http_routes import route_http  # type: ignore[import-not-found]
    from api.ws_routes import handle_socket_message  # type: ignore[import-not-found]
    from auth.jwt import JwtValidationError, decode_jwt, extract_bearer_or_cookie  # type: ignore[import-not-found]
    from auth.jwt import resolve_role as _resolve_jwt_role  # type: ignore[import-not-found]
    from bridge.hijack import HijackCoordinator  # type: ignore[import-not-found]
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
        self.browser_resume_tokens: dict[str, str] = {}
        self.last_snapshot: dict[str, Any] | None = None
        self.last_analysis: str | None = None
        self.input_mode: str = "hijack"
        self.lifecycle_state: str = "stopped"
        self._deleted_at: float | None = None
        self.meta: dict[str, Any] = {
            "display_name": self.worker_id,
            "connector_type": "unknown",
            "created_at": 0.0,
            "tags": [],
            "visibility": "public",
            "owner": None,
        }
        self._meta_loaded: bool = False
        self._tunnel_worker_token: str | None = None
        self._share_token: str | None = None
        self._control_token: str | None = None
        self._issued_ip: str | None = None

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

    async def _ensure_meta(self) -> None:
        """Lazy-load session metadata from KV on first contact, persist to SQLite."""
        if self._meta_loaded:
            return
        self._meta_loaded = True
        kv = getattr(self.env, "SESSION_REGISTRY", None)
        if kv is None:
            return
        try:
            raw = await kv.get(f"session:{self.worker_id}")
            if raw is not None:
                data = json.loads(str(raw))
                self.meta = {
                    "display_name": data.get("display_name") or self.worker_id,
                    "connector_type": data.get("connector_type") or "unknown",
                    "created_at": float(data.get("created_at") or time.time()),
                    "tags": data.get("tags") or [],
                    "visibility": data.get("visibility") or "public",
                    "owner": data.get("owner"),
                }
                self._tunnel_worker_token = str(data.get("worker_token") or "") or None
                self._share_token = str(data.get("share_token") or "") or None
                self._control_token = str(data.get("control_token") or "") or None
                self._issued_ip = str(data.get("issued_ip") or "") or None
        except Exception:
            logger.debug("_ensure_meta kv read failed for %s", self.worker_id)
        self.store.save_session_meta(self.worker_id, self.meta)

    def _share_role_for_request(self, request: object) -> str | None:
        transport = str(getattr(self.config, "tunnel_token_transport", "both"))
        ip_binding = bool(getattr(self.config, "tunnel_ip_binding", False))

        token = None
        if transport != "cookie":  # "query" or "both"
            try:
                qs = parse_qs(urlparse(str(request.url)).query)  # type: ignore[attr-defined]
                token = ((qs.get("token", []) + qs.get("access_token", [])) or [None])[0]
            except Exception as exc:
                logger.debug("failed to parse share token: %s", exc)
        # Cookie fallback: uterm_tunnel_{worker_id}
        if not token and transport != "query":  # "cookie" or "both"
            try:
                from http.cookies import SimpleCookie

                cookie_header = str(request.headers.get("cookie") or request.headers.get("Cookie") or "")  # type: ignore[union-attr]
                cookies = SimpleCookie(cookie_header)
                cookie_key = f"uterm_tunnel_{self.worker_id}"
                if cookie_key in cookies:
                    token = cookies[cookie_key].value
            except Exception:
                pass
        if not token:
            return None

        role: str | None = None
        if self._control_token and secrets.compare_digest(token, self._control_token):
            role = "admin"
        elif self._share_token and secrets.compare_digest(token, self._share_token):
            role = "viewer"

        if role is None:
            return None

        if ip_binding:
            issued_ip = self._issued_ip or ""
            client_ip = ""
            try:
                client_ip = str(request.headers.get("CF-Connecting-IP") or "")  # type: ignore[union-attr]
            except Exception:
                pass
            if issued_ip and client_ip != issued_ip:
                return None

        return role

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
        share_role = self._share_role_for_request(request)
        if share_role is not None:
            return None, None
        # CF Access Service Auth: if the request carries a service token
        # header, CF Access already validated it — trust the request.
        try:
            cf_client_id = str(request.headers.get("CF-Access-Client-Id") or "")  # type: ignore[union-attr]
            if len(cf_client_id) > 0:
                return None, None
        except Exception:
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
        """Return the caller's role string based on JWT, ownership, or auth mode.

        Returns ``"admin"`` in ``none``/``dev`` mode (open access). In ``jwt`` mode,
        decodes the token and returns ``"admin"``, ``"operator"``, or ``"viewer"``.
        Owners of a private session are elevated to ``"operator"`` when their
        JWT role is lower — matching the hosted FastAPI server's
        ``resolve_browser_role``.  Without this elevation an owner with a
        ``viewer``-role JWT could read their session via the visibility check
        but would get 403 on every mutation route (mode/hijack/…).
        Falls back to ``"viewer"`` if the token is missing or invalid (the token
        was already validated in ``fetch()``; this is only for role extraction).
        """
        if self.config.jwt.mode in {"none", "dev"}:
            return "admin"
        share_role = self._share_role_for_request(request)
        if share_role is not None:
            return share_role
        token = self._extract_token(request)
        if not token:
            return "viewer"
        try:
            principal = await decode_jwt(token, self.config.jwt)
        except JwtValidationError:
            return "viewer"
        # Other exceptions (e.g. network errors fetching JWKS) propagate so the
        # caller returns a 5xx rather than silently downgrading the caller to viewer.
        jwt_role = _resolve_jwt_role(principal)
        if jwt_role == "admin":
            return "admin"
        owner = self.meta.get("owner")
        if owner is not None and principal.subject_id == owner:
            return "operator"
        return jwt_role

    async def browser_subject_for_request(self, request: object) -> str | None:
        """Return the JWT subject_id for the caller, or ``None`` in open-access modes.

        Used by session route handlers to check per-session ownership.
        """
        if self.config.jwt.mode in {"none", "dev"}:
            return None
        token = self._extract_token(request)
        if not token:
            return None
        try:
            principal = await decode_jwt(token, self.config.jwt)
            return principal.subject_id
        except JwtValidationError:
            return None

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
        for prefix in ("/ws/worker/", "/ws/browser/", "/ws/raw/", "/tunnel/", "/worker/", "/api/sessions/"):
            if path.startswith(prefix):
                segment = path[len(prefix) :].split("/")[0]
                if segment:
                    self.worker_id = segment
                    return

    async def fetch(self, request: object) -> Response:
        # Resolve worker_id from URL when ctx.id.name() is unavailable (CF Python runtime bug).
        self._lazy_init_worker_id(request)
        if self._deleted_at is not None:
            return Response(
                json.dumps({"error": "not_found", "path": urlparse(str(request.url)).path}, ensure_ascii=True),
                status=404,
                headers={"content-type": "application/json"},
            )
        await self._ensure_meta()

        # Parse URL once — reused for worker WS check and socket role routing.
        upgrade_header = str(request.headers.get("Upgrade") or "").lower()  # type: ignore[attr-defined]
        path = urlparse(str(request.url)).path  # type: ignore[attr-defined]

        # Worker WS connections authenticate with a bearer token, not JWT.
        # When worker_bearer_token is None (dev/none mode), this block is
        # skipped entirely and the request falls through to _resolve_principal()
        # which permits all callers in those modes.  In JWT mode, from_env()
        # guarantees worker_bearer_token is set (ValueError otherwise).
        _is_worker_ws = upgrade_header == "websocket" and path.startswith(("/ws/worker/", "/tunnel/", "/ws/raw/"))
        if _is_worker_ws and self.config.worker_bearer_token:
            # CF Access service tokens bypass worker bearer token check.
            _cf_client = str(request.headers.get("CF-Access-Client-Id") or "")  # type: ignore[union-attr]
            if _cf_client.endswith(".access"):
                _principal, auth_error = None, None
            else:
                token = extract_bearer_or_cookie(request)
                valid_worker_token = False
                auth_type = "none"
                if token and secrets.compare_digest(token, self.config.worker_bearer_token):
                    valid_worker_token = True
                    auth_type = "global_bearer"
                if token and self._tunnel_worker_token and secrets.compare_digest(token, self._tunnel_worker_token):
                    valid_worker_token = True
                    auth_type = "tunnel_session"
                if not valid_worker_token:
                    logger.info("tunnel_token_validated worker_id=%s valid=false", self.worker_id)
                    return Response(
                        json.dumps({"error": "worker authentication required"}),
                        status=403,
                        headers={"content-type": "application/json"},
                    )
                logger.info("tunnel_token_validated worker_id=%s auth_type=%s", self.worker_id, auth_type)
                _principal, auth_error = None, None
        else:
            _principal, auth_error = await self._resolve_principal(request)
            if auth_error is not None:
                return auth_error
        if upgrade_header == "websocket":
            from js import WebSocketPair  # type: ignore[import-not-found]

            socket_role = "browser"
            if path.startswith(("/ws/worker/", "/tunnel/")):
                socket_role = "worker"
            elif path.startswith("/ws/raw/"):
                socket_role = "raw"

            # Resolve browser role from JWT (defaults to "admin" in dev/none mode).
            # Workers and raw sockets authenticate via bearer token, not JWT —
            # they are unconditionally admitted and assigned "admin" role.
            if socket_role in ("worker", "raw"):
                browser_role = "admin"
            else:
                browser_role = await self.browser_role_for_request(request)
                # Enforce session visibility before upgrading browser WebSockets.
                # Only browser sockets carry a JWT and require visibility checks.
                visibility = str(self.meta.get("visibility") or "public")
                if visibility != "public":
                    if browser_role != "admin":
                        subject = await self.browser_subject_for_request(request)
                        owner = self.meta.get("owner")
                        permitted = subject is not None and subject == owner
                        if not permitted and visibility == "operator":
                            permitted = browser_role == "operator"
                        if not permitted:
                            return Response(
                                json.dumps({"error": "forbidden"}),
                                status=403,
                                headers={"content-type": "application/json"},
                            )

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
                        meta=self.meta,
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
                self.browser_resume_tokens[self.ws_key(server)] = resume_token
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
                                "presence_enabled": bool(self.meta.get("presence")),
                                "protocol_version": CURRENT_PROTOCOL_VERSION,
                                "ts": time.time(),
                            }
                        )
                    )
                except Exception as exc:
                    logger.warning("failed to send hello from fetch(): %s", exc)
                try:
                    await self._maybe_send_presence_sync(server, exclude_self=False)
                except Exception as exc:  # pragma: no cover — requires real WS upgrade + presence
                    logger.warning("failed to send presence_sync from fetch(): %s", exc)

            return Response(None, status=101, web_socket=client)
        return await route_http(self, request)

    async def webSocketOpen(self, ws: CFWebSocket) -> None:  # noqa: N802
        if self._deleted_at is not None:
            with contextlib.suppress(Exception):
                ws.close(1001, "session deleted")
            return
        ws_id = self.ws_key(ws)
        role = self._socket_role(ws)
        # Check before _register_socket so we can detect fetch()-initialized browser sockets.
        # fetch() sends hello before the 101 response; webSocketOpen() fires after the upgrade.
        # For the normal (non-hibernation) path the socket is already registered — skip hello.
        already_initialized = ws_id in self.browser_sockets
        self._register_socket(ws, role)
        if role == "worker":
            self.worker_ws = ws
            self.lifecycle_state = "running"
            await self.broadcast_worker_frame(
                {"type": "worker_connected", "worker_id": self.worker_id, "ts": time.time()}
            )
            await update_kv_session(
                self.env,
                self.worker_id,
                connected=True,
                hijacked=self.hijack.session is not None,
                input_mode=self.input_mode,
                meta=self.meta,
            )
        elif role == "raw":
            self.raw_sockets[ws_id] = ws
            if self.last_snapshot is not None and isinstance(self.last_snapshot.get("screen"), str):
                await self._send_text(ws, str(self.last_snapshot.get("screen")))
        else:
            self.browser_sockets[ws_id] = ws
            browser_role = self._socket_browser_role(ws)
            if not already_initialized:
                # Hibernation-restore path: fetch() did not run for this connection, so
                # send hello here.  For normal upgrades fetch() already sent it.
                _open_resume_token = secrets.token_urlsafe(32)
                _open_resume_ttl = float(getattr(self.config, "resume_ttl_s", 300))
                self.store.create_resume_token(_open_resume_token, self.worker_id, browser_role, _open_resume_ttl)
                self.browser_resume_tokens[ws_id] = _open_resume_token
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
                        "presence_enabled": bool(self.meta.get("presence")),
                        "protocol_version": CURRENT_PROTOCOL_VERSION,
                        "ts": time.time(),
                    },
                )
            await self._maybe_send_presence_sync(ws, exclude_self=True)
            await self.send_hijack_state(ws)
            if self.last_snapshot is not None:
                await self.send_ws(ws, self.last_snapshot)
            # For ushell sessions, broadcast worker_connected + welcome on first browser join.
            await on_browser_connected(self)

    async def webSocketMessage(self, ws: CFWebSocket, message: Any) -> None:  # noqa: N802
        if self._deleted_at is not None:
            with contextlib.suppress(Exception):
                ws.close(1001, "session deleted")
            return
        role = self._socket_role(ws)
        self._register_socket(ws, role)
        if role == "raw":
            payload = (
                message.decode("latin-1", errors="replace") if isinstance(message, (bytes, bytearray)) else str(message)
            )
            await self.push_worker_input(payload)
            return

        # Tunnel protocol: binary frames from the tunnel agent (worker role).
        # In Pyodide, JS ArrayBuffer/Uint8Array arrives as a JsProxy, not Python bytes.
        # Convert via to_py() or to_bytes() before checking isinstance.
        _bin = message
        if hasattr(_bin, "to_py"):  # pragma: no cover — Pyodide JsProxy only
            _bin = _bin.to_py()
        elif hasattr(_bin, "to_bytes"):  # pragma: no cover — Pyodide JsProxy only
            _bin = _bin.to_bytes()
        if isinstance(_bin, (bytes, bytearray, memoryview)) and role == "worker":
            try:
                from provide.terminal.cloudflare.api.tunnel_routes import handle_tunnel_message
            except ImportError:  # pragma: no cover
                from api.tunnel_routes import handle_tunnel_message  # type: ignore[import-not-found]

            await handle_tunnel_message(self, ws, bytes(_bin))
            return

        raw = message if isinstance(message, str) else str(message)
        await handle_socket_message(self, ws, raw, is_worker=(role == "worker"))

    async def webSocketClose(self, ws: CFWebSocket, code: int, reason: str, was_clean: bool = True) -> None:  # noqa: N802
        _ = (code, reason, was_clean)
        deleted = self._deleted_at is not None
        # Use _socket_role() instead of `ws is self.worker_ws` — after hibernation,
        # self.worker_ws is None so the identity check would always be False.
        role = self._socket_role(ws)
        wid = self._socket_worker_id(ws)
        if role == "browser" and not deleted:
            ws_id = self.ws_key(ws)
            if self.meta.get("presence"):
                await self.broadcast_to_browsers({"type": "presence_leave", "user_id": ws_id, "ts": time.time()})
            # Mark the resume token as hijack owner before removing socket, so the
            # browser can reclaim ownership on reconnect.
            if ws_id in self.browser_hijack_owner:
                token = self.browser_resume_tokens.get(ws_id)
                if token:
                    self.store.mark_resume_hijack_owner(token, True)
        self._remove_ws(ws)
        if role == "worker":
            if not deleted:
                self.lifecycle_state = "stopped"
                await self.broadcast_worker_frame({"type": "worker_disconnected", "worker_id": wid, "ts": time.time()})
            await update_kv_session(self.env, wid, connected=False)

    async def webSocketError(self, ws: CFWebSocket, error: Any) -> None:  # noqa: N802
        role = self._socket_role(ws)
        wid = self._socket_worker_id(ws)
        logger.warning("ws_error worker_id=%s role=%s error=%s", wid, role, error)
        deleted = self._deleted_at is not None
        if role == "browser" and not deleted:
            ws_id = self.ws_key(ws)
            if self.meta.get("presence"):
                await self.broadcast_to_browsers({"type": "presence_leave", "user_id": ws_id, "ts": time.time()})
            if ws_id in self.browser_hijack_owner:
                token = self.browser_resume_tokens.get(ws_id)
                if token:
                    self.store.mark_resume_hijack_owner(token, True)
        self._remove_ws(ws)
        if role == "worker":
            if not deleted:
                self.lifecycle_state = "error"
                await self.broadcast_worker_frame({"type": "worker_disconnected", "worker_id": wid, "ts": time.time()})
            await update_kv_session(self.env, wid, connected=False)
