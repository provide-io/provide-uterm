"""Cloudflare Worker entrypoint package.

Topical split of the former monolithic ``entry.py``:

* :mod:`.fallback_stubs` — Pyodide / flat-layout import strategy.
* :mod:`.spa`            — SPA route resolution and HTML shell rendering.
* :mod:`.share_tokens`   — share-token cookie helpers.
* :mod:`.auth`           — JWT enforcement, CF Access service tokens, principal decoding.
* :mod:`.security`       — security-header resolution / application.
* :mod:`.handlers`       — API handlers and the ``_route_request`` dispatcher.
* :mod:`.registry`       — ``Default`` ``WorkerEntrypoint`` and worker-id matcher.

This ``__init__`` re-exports only the genuine public surface plus the
Pyodide / flat-layout fallback symbols re-exported from
:mod:`.fallback_stubs`.  Internal/private symbols live in their topical
submodules and are imported directly from there by callers and tests.
"""

from __future__ import annotations

from provide.uterm.cloudflare.entry.fallback_stubs import (
    CloudflareConfig,
    JwtValidationError,
    Response,
    SessionRuntime,
    WorkerEntrypoint,
    decode_jwt,
    delete_kv_session,
    extract_bearer_or_cookie,
    get_kv_session,
    json_response,
    list_kv_sessions,
    read_asset_text,
    serve_asset,
)
from provide.uterm.cloudflare.entry.registry import (
    Default,
    ProvideTerminalCloudflareWorker,
)

__all__ = [
    "CloudflareConfig",
    "Default",
    "JwtValidationError",
    "ProvideTerminalCloudflareWorker",
    "Response",
    "SessionRuntime",
    "WorkerEntrypoint",
    "decode_jwt",
    "delete_kv_session",
    "extract_bearer_or_cookie",
    "get_kv_session",
    "json_response",
    "list_kv_sessions",
    "read_asset_text",
    "serve_asset",
]
