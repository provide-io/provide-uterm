"""Worker registration: route-pattern matching, ``Default`` handler class.

Defines the worker-id extractor used by the DO proxy path and the
``Default`` ``WorkerEntrypoint`` subclass that Cloudflare's Pyodide
validation phase detects as the registered HTTP handler.
"""

from __future__ import annotations

import logging
import re

from provide.terminal.cloudflare.entry.fallback_stubs import (
    CloudflareConfig,
    WorkerEntrypoint,
    _import_error,
)
from provide.terminal.cloudflare.entry.handlers import _route_request
from provide.terminal.cloudflare.entry.security import _apply_security_headers

logger = logging.getLogger(__name__)

_WORKER_ROUTE_PATTERNS = (
    re.compile(r"^/ws/browser/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/term$"),
    re.compile(r"^/ws/worker/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/term$"),
    re.compile(r"^/ws/raw/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/term$"),
    re.compile(r"^/tunnel/(?P<worker_id>[a-zA-Z0-9_-]{1,64})$"),
    re.compile(r"^/worker/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/hijack(?:/.*)?$"),
    re.compile(r"^/worker/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/(?:input_mode|disconnect_worker)$"),
    re.compile(
        r"^/api/sessions/(?P<worker_id>[a-zA-Z0-9_-]{1,64})(?:/(?:snapshot|events|mode|clear|analyze|restart|recording(?:/(?:entries|download))?))?$"
    ),
    re.compile(r"^/api/sessions/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/events/stream$"),
    re.compile(r"^/api/sessions/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/webhooks(?:/[a-zA-Z0-9_-]{1,64})?$"),
)


def _extract_worker_id(path: str) -> str | None:
    """Return the worker-id captured by any DO-proxied route, or ``None``."""
    for pattern in _WORKER_ROUTE_PATTERNS:
        match = pattern.match(path)
        if match:
            return str(match.group("worker_id"))
    return None


class Default(WorkerEntrypoint):  # type: ignore[misc]
    """Default HTTP handler exposed to the Cloudflare Workers runtime."""

    async def fetch(self, request: object) -> object:
        if not hasattr(self, "_config"):
            if _import_error:  # pragma: no cover
                logger.error("IMPORT_FALLBACK:\n%s", _import_error)  # pragma: no cover
            self._config = CloudflareConfig.from_env(self.env)
        response = await _route_request(request, self.env, self._config)
        if getattr(response, "status", None) != 101:
            _apply_security_headers(response, self._config)
        return response


ProvideTerminalCloudflareWorker = Default


__all__ = [
    "_WORKER_ROUTE_PATTERNS",
    "Default",
    "ProvideTerminalCloudflareWorker",
    "_extract_worker_id",
]
