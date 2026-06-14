# mypy: ignore-errors
"""Pyodide / Cloudflare Workers fallback import bootstrapping.

This module concentrates the triple-import strategy used by the Cloudflare
Worker entrypoint:

1. **Real CF runtime** — ``from workers import ...`` resolves to the actual
   handler base classes Cloudflare's Pyodide validation phase needs.
2. **Installed package layout** — ``from provide.uterm.cloudflare.X import Y``
   is the canonical path used in tests and local dev.
3. **Flat layout** — ``from X import Y`` is the fallback for the wrangler-
   flattened ``/session/`` tree where the package has been collapsed onto
   ``sys.path`` by the vendor build.
4. **Pyodide validation stubs** — last-resort no-op stubs that let
   ``Default`` / ``SessionRuntime`` register as event handlers even if the
   rest of the package failed to load.

All names this module sets (``Response``, ``WorkerEntrypoint``,
``CloudflareConfig``, ``SessionRuntime``, ``decode_jwt`` ...) are re-exported
through ``provide.uterm.cloudflare.entry`` and consumed by the topical
submodules.  Suppression density is centralized here via the file-level
``# mypy: ignore-errors`` so the rest of the package can stay strict.
"""

from __future__ import annotations

import sys
import traceback as _tb
from pathlib import Path

# ---------------------------------------------------------------------------
# Stage 1: handler base classes — MUST come from the real CF runtime so
# Cloudflare's Pyodide validation phase detects Default / SessionRuntime as
# registered event handlers.
# ---------------------------------------------------------------------------
_DurableObject: type = object
try:
    from workers import DurableObject as _DurableObject  # pragma: no cover  # ty:ignore[unresolved-import]
    from workers import (  # pragma: no cover  # ty:ignore[unresolved-import]
        Response,
        WorkerEntrypoint,
    )
except ImportError:
    # Outside CF runtime (tests / local dev): stubs loaded below from cf_types.
    Response = None
    WorkerEntrypoint = None

# ---------------------------------------------------------------------------
# sys.path bootstrapping for the Pyodide flat-layout.  Wrangler may flatten
# src/ so that entry.py sits at /session/ while the package is at
# /session/provide.uterm.cloudflare/.  Add /session/, /session/metadata/,
# and any python_modules siblings to sys.path before attempting the import.
# ---------------------------------------------------------------------------
_current_file = Path(__file__).resolve()
_current_dir = str(_current_file.parent.parent)  # .../provide/uterm/cloudflare/
_parent_dir = str(_current_file.parent.parent.parent)  # .../provide/uterm/
_python_module_candidates: list[Path] = []
for _p in _current_file.parents:
    _python_module_candidates.append(_p / "python_modules")

_import_error: str | None = None

for _path in [_parent_dir, _current_dir, *[str(p) for p in _python_module_candidates]]:
    if (
        _path not in sys.path
    ):  # pragma: no branch — module-level bootstrap; "already in sys.path" branch only fires in Pyodide flat-layout reloads
        sys.path.insert(0, _path)

# ---------------------------------------------------------------------------
# Stage 2: prefer the installed package; fall back to flat layout; final
# fallback assigns Pyodide-validation stubs.
# ---------------------------------------------------------------------------
try:
    from provide.uterm.cloudflare.auth.jwt import (
        JwtValidationError,
        decode_jwt,
        extract_bearer_or_cookie,
    )
    from provide.uterm.cloudflare.cf_types import (
        Response,
        WorkerEntrypoint,
        json_response,
    )
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.do.session_runtime import SessionRuntime
    from provide.uterm.cloudflare.state.registry import (
        delete_kv_session,
        get_kv_session,
        list_kv_sessions,
    )
    from provide.uterm.cloudflare.ui.assets import read_asset_text, serve_asset
except ImportError:  # pragma: no cover — Pyodide flat-layout / validation phase only
    try:
        from auth.jwt import (  # type: ignore[import-not-found]  # ty:ignore[unresolved-import]
            JwtValidationError,
            decode_jwt,
            extract_bearer_or_cookie,
        )
        from cf_types import (  # type: ignore[import-not-found]  # ty:ignore[unresolved-import]
            Response,
            WorkerEntrypoint,
            json_response,
        )
        from config import CloudflareConfig  # type: ignore[import-not-found]  # ty:ignore[unresolved-import]
        from do.session_runtime import SessionRuntime  # type: ignore[import-not-found]  # ty:ignore[unresolved-import]
        from state.registry import (  # type: ignore[import-not-found]  # ty:ignore[unresolved-import]
            delete_kv_session,
            get_kv_session,
            list_kv_sessions,
        )
        from ui.assets import (  # type: ignore[import-not-found]  # ty:ignore[unresolved-import]
            read_asset_text,
            serve_asset,
        )
    except Exception as _exc2:  # pragma: no cover — Pyodide validation phase only
        # Last resort for Pyodide validation phase — stubs for non-handler imports.
        # WorkerEntrypoint / Response / DurableObject came from `workers` above,
        # so handler registration always succeeds.
        _import_error = _tb.format_exc()
        JwtValidationError = Exception  # ty:ignore[conflicting-declarations]

        def decode_jwt(*_a: object, **_k: object) -> None:
            return None

        def extract_bearer_or_cookie(*_a: object, **_k: object) -> None:
            return None

        def json_response(payload: object, status: int = 200, headers: object | None = None):
            if Response is None:
                return None
            return Response.json(payload, status=status, headers=headers)

        try:
            from config import CloudflareConfig  # type: ignore[import-not-found]  # ty:ignore[unresolved-import]
        except Exception:
            CloudflareConfig = object  # ty:ignore[conflicting-declarations]

        try:
            from do.session_runtime import (  # ty:ignore[unresolved-import]
                SessionRuntime,  # type: ignore[import-not-found]
            )
        except Exception:

            class SessionRuntime(_DurableObject):
                """Stub DO for validation phase — real impl loaded at runtime."""

                async def fetch(self, _request):
                    return Response.json({"error": "not initialized"}, status=503)  # ty:ignore[unresolved-attribute]

        def get_kv_session(*_a: object, **_k: object) -> None:
            return None

        def delete_kv_session(*_a: object, **_k: object) -> None:
            return None

        def list_kv_sessions(*_a: object, **_k: object) -> None:
            return None

        def read_asset_text(*_a: object, **_k: object) -> None:
            return None

        def serve_asset(*_a: object, **_k: object) -> None:
            return None


__all__ = [
    "CloudflareConfig",
    "JwtValidationError",
    "Response",
    "SessionRuntime",
    "WorkerEntrypoint",
    "_DurableObject",
    "_import_error",
    "decode_jwt",
    "delete_kv_session",
    "extract_bearer_or_cookie",
    "get_kv_session",
    "json_response",
    "list_kv_sessions",
    "read_asset_text",
    "serve_asset",
]
