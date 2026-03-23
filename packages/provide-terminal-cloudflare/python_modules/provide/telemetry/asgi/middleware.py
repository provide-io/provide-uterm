# SPDX-FileCopyrightText: Copyright (C) 2026 MindTenet LLC
# SPDX-License-Identifier: Apache-2.0
# SPDX-Comment: Part of Provide Telemetry.
#

"""ASGI middleware for telemetry context propagation."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from provide.telemetry.cardinality import register_cardinality_limit
from provide.telemetry.headers import get_header
from provide.telemetry.logger.context import bind_context, reset_context, save_context
from provide.telemetry.logger.core import get_logger
from provide.telemetry.propagation import bind_propagation_context, clear_propagation_context, extract_w3c_context
from provide.telemetry.schema.events import event_name
from provide.telemetry.slo import record_red_metrics

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class TelemetryMiddleware:
    def __init__(
        self,
        app: Callable[[Scope, Receive, Send], Awaitable[None]],
        *,
        auto_slo: bool = False,  # pragma: no mutate
    ) -> None:
        self.app = app
        self.auto_slo = auto_slo
        self._logger = get_logger("provide.asgi") if auto_slo else None
        if auto_slo:
            register_cardinality_limit("route", max_values=200)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        request_id = _extract_header(scope, b"x-request-id") or uuid.uuid4().hex
        session_id = _extract_header(scope, b"x-session-id")
        ctx_token = save_context()
        bind_context(request_id=request_id)
        if session_id is not None:
            bind_context(session_id=session_id)
        bind_propagation_context(extract_w3c_context(scope))
        status_code = 500
        started = time.perf_counter()

        async def _wrapped_send(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status = message.get("status")
                if isinstance(status, int):
                    status_code = status
            elif message.get("type") == "websocket.accept":
                status_code = 101
            elif message.get("type") == "websocket.close":
                code = message.get("code")
                if isinstance(code, int):
                    status_code = code
            await send(message)

        try:
            await self.app(scope, receive, _wrapped_send if self.auto_slo else send)
        except Exception as exc:
            if self.auto_slo and self._logger is not None:
                scope_type = str(scope.get("type", "http"))  # pragma: no mutate
                self._logger.error(
                    event_name(scope_type, "request", "unhandled_exception"),
                    exc_info=True,
                    exc_name=exc.__class__.__name__,
                    path=str(scope.get("path", "unknown")),
                )
            raise
        finally:
            if self.auto_slo:
                duration_ms = (time.perf_counter() - started) * 1000.0  # pragma: no mutate
                route = _resolve_route(scope)
                method = str(scope.get("method", "UNKNOWN")) if scope.get("type") == "http" else "WS"
                record_red_metrics(route=route, method=method, status_code=status_code, duration_ms=duration_ms)
            clear_propagation_context()
            reset_context(ctx_token)


_DYNAMIC_SEGMENT = re.compile(
    r"/(?:"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"  # UUID
    r"|[0-9a-fA-F]{24,32}"  # hex object IDs
    r"|[0-9]+"  # numeric IDs
    r")(?=/|$)"
)


def _normalize_path(raw: str) -> str:
    return _DYNAMIC_SEGMENT.sub("/{id}", raw)


def _resolve_route(scope: Scope) -> str:
    route = scope.get("route")
    if route is not None:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            return path
    return _normalize_path(str(scope.get("path", "unknown")))


def _extract_header(scope: Scope, key: bytes) -> str | None:
    return get_header(scope, key)
