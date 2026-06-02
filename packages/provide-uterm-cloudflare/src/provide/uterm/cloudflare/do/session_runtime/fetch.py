#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""HTTP/WebSocket dispatch for SessionRuntime.

Houses ``fetch()``, ``_fetch_impl()``, and the ``_lazy_init_worker_id`` helper.
The fetch path resolves auth, performs WS upgrades, and routes HTTP requests
into the API layer.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

from provide.telemetry import get_tracer

from provide.uterm.bridge.contracts import (
    CURRENT_PROTOCOL_VERSION,
    MAX_PROTOCOL_VERSION,
    MIN_PROTOCOL_VERSION,
    PREFERRED_PROTOCOL_VERSION,
)
from provide.uterm.control_channel import encode_control

if TYPE_CHECKING:
    from provide.uterm.cloudflare.api.http_routes import route_http
    from provide.uterm.cloudflare.auth.jwt import extract_bearer_or_cookie
    from provide.uterm.cloudflare.cf_types import Response
    from provide.uterm.cloudflare.contracts import RuntimeProtocol
    from provide.uterm.cloudflare.do.ushell import init_ushell
    from provide.uterm.cloudflare.state.registry import update_kv_session
else:
    try:
        from provide.uterm.cloudflare.api.http_routes import route_http
        from provide.uterm.cloudflare.auth.jwt import extract_bearer_or_cookie
        from provide.uterm.cloudflare.cf_types import Response
        from provide.uterm.cloudflare.do.ushell import init_ushell
        from provide.uterm.cloudflare.state.registry import update_kv_session
    except Exception:  # pragma: no cover
        from api.http_routes import route_http  # type: ignore[import-not-found,no-redef]
        from auth.jwt import extract_bearer_or_cookie  # type: ignore[import-not-found,no-redef]
        from cf_types import Response  # type: ignore[import-not-found]
        from do.ushell import init_ushell  # type: ignore[import-not-found,no-redef]
        from state.registry import update_kv_session  # type: ignore[import-not-found,no-redef]

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)


class _FetchMixin:
    """Mixin providing the DO fetch dispatch and WS upgrade for SessionRuntime."""

    if TYPE_CHECKING:
        worker_id: str
        config: Any
        env: Any
        ctx: Any
        meta: dict[str, Any]
        store: Any
        hijack: Any
        input_mode: str
        worker_ws: Any
        browser_resume_tokens: dict[str, str]
        _deleted_at: float | None
        _tunnel_worker_token_hash: str | None
        _ushell: Any

        # Methods from other mixins
        async def _ensure_meta(self) -> None: ...
        async def _ensure_credentials(self) -> None: ...
        async def resolve_principal(self, request: Any) -> tuple[Any, Response | None]: ...
        async def browser_role_for_request(self, request: Any) -> str: ...
        async def browser_subject_for_request(self, request: Any) -> str | None: ...
        def _register_socket(self, ws: Any, role: str) -> None: ...
        def ws_key(self, ws: Any) -> str: ...
        async def _maybe_send_presence_sync(self, ws: Any, *, exclude_self: bool = False) -> None: ...

    def _lazy_init_worker_id(self, request: Any) -> None:
        """Update worker_id from the request URL when ctx.id.name() returned 'default'.

        Called at the start of fetch() so KV writes and state operations use
        the real worker_id extracted from the URL path.
        """
        if self.worker_id != "default":
            return
        try:
            path = urlparse(str(request.url)).path
        except Exception:
            return
        for prefix in ("/ws/worker/", "/ws/browser/", "/ws/raw/", "/tunnel/", "/worker/", "/api/sessions/"):
            if path.startswith(prefix):
                segment = path[len(prefix) :].split("/")[0]
                if segment:
                    self.worker_id = segment
                    return

    async def fetch(self, request: Any) -> Response:
        with tracer.start_as_current_span("uterm.cloudflare.fetch"):
            return await self._fetch_impl(request)

    async def _fetch_impl(self, request: Any) -> Response:
        # Resolve worker_id from URL when ctx.id.name() is unavailable (CF Python runtime bug).
        self._lazy_init_worker_id(request)
        if self._deleted_at is not None:
            return Response(
                json.dumps({"error": "not_found", "path": urlparse(str(request.url)).path}, ensure_ascii=True),
                status=404,
                headers={"content-type": "application/json"},
            )
        await self._ensure_meta()  # type: ignore[attr-defined]
        await self._ensure_credentials()  # type: ignore[attr-defined]

        # Parse URL once — reused for worker WS check and socket role routing.
        upgrade_header = str(request.headers.get("Upgrade") or "").lower()
        path = urlparse(str(request.url)).path

        # Worker WS connections authenticate with a bearer token, not JWT.
        # When worker_bearer_token is None (dev/none mode), this block is
        # skipped entirely and the request falls through to _resolve_principal()
        # which permits all callers in those modes.  In JWT mode, from_env()
        # guarantees worker_bearer_token is set (ValueError otherwise).
        _is_worker_ws = upgrade_header == "websocket" and path.startswith(("/ws/worker/", "/tunnel/", "/ws/raw/"))
        if _is_worker_ws and self.config.worker_bearer_token:
            token = extract_bearer_or_cookie(request)
            valid_worker_token = False
            auth_type = "none"
            if token and secrets.compare_digest(token, self.config.worker_bearer_token):
                valid_worker_token = True
                auth_type = "global_bearer"
            # Constant-time hash compare against the stored BLAKE2b digest.
            from provide.uterm.tunnel.token_hash import verify_token

            if token and self._tunnel_worker_token_hash and verify_token(token, self._tunnel_worker_token_hash):
                valid_worker_token = True
                auth_type = "tunnel_session"
            if not valid_worker_token:
                logger.info("tunnel_token_validated worker_id=%s valid=false", self.worker_id)
                return Response(
                    json.dumps({"error": "worker authentication required"}),
                    status=403,
                    headers={"content-type": "application/json"},
                )
            logger.info(
                "tunnel_token_validated worker_id=%s auth_type=%s",
                self.worker_id,
                auth_type,
            )
            _principal, auth_error = None, None
        else:
            _principal, auth_error = await self.resolve_principal(request)  # type: ignore[attr-defined]
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
                browser_role = await self.browser_role_for_request(request)  # type: ignore[attr-defined]
                # Enforce session visibility before upgrading browser WebSockets.
                # Only browser sockets carry a JWT and require visibility checks.
                visibility = str(self.meta.get("visibility") or "public")
                if visibility != "public" and browser_role != "admin":
                    subject = await self.browser_subject_for_request(request)  # type: ignore[attr-defined]
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

            client, server = WebSocketPair.new().object_values()  # type: ignore[union-attr]
            self.ctx.acceptWebSocket(server)  # type: ignore[attr-defined]
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
            self._register_socket(server, socket_role)  # type: ignore[attr-defined]  # server is Any from WebSocketPair

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
                self.browser_resume_tokens[self.ws_key(server)] = resume_token  # type: ignore[attr-defined]  # server is Any
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
                                "protocol": {
                                    "selected": PREFERRED_PROTOCOL_VERSION,
                                    "server_min": MIN_PROTOCOL_VERSION,
                                    "server_max": MAX_PROTOCOL_VERSION,
                                },
                                "ts": time.time(),
                            }
                        )
                    )
                except Exception as exc:
                    logger.warning("failed to send hello from fetch(): %s", exc)
                try:
                    await self._maybe_send_presence_sync(server, exclude_self=False)  # type: ignore[attr-defined]
                except Exception as exc:  # pragma: no cover — requires real WS upgrade + presence
                    logger.warning("failed to send presence_sync from fetch(): %s", exc)

            return Response(None, status=101, web_socket=client)
        result = await route_http(cast("RuntimeProtocol", self), request)
        return cast("Response", result)
