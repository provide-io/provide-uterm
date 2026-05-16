#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Core ``SessionRuntime`` Durable Object class shell.

Composes auth, fetch, lifecycle, I/O, and WS-helper mixins.  Holds the
``__init__`` and the meta-loading helpers; everything else is delegated to
the mixin modules in this package.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from provide.telemetry import get_tracer

try:
    from provide.uterm.cloudflare.bridge.hijack import HijackCoordinator
    from provide.uterm.cloudflare.cf_types import CFWebSocket, DurableObject
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.state.store import SqliteStateStore
except Exception:  # pragma: no cover
    from bridge.hijack import HijackCoordinator  # type: ignore[import-not-found]
    from cf_types import CFWebSocket, DurableObject  # type: ignore[import-not-found]
    from config import CloudflareConfig  # type: ignore[import-not-found]
    from state.store import SqliteStateStore  # type: ignore[import-not-found]

from .auth import _AuthMixin
from .fetch import _FetchMixin
from .io import _SessionRuntimeIoMixin
from .lifecycle import _LifecycleMixin
from .ws_helpers import _WsHelperMixin

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)


class SessionRuntime(
    _SessionRuntimeIoMixin,
    _WsHelperMixin,
    _AuthMixin,
    _FetchMixin,
    _LifecycleMixin,
    DurableObject,
):
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
        self._queue_bytes = 0
        self.max_buffer_bytes = self.config.limits.max_buffer_bytes
        self.last_snapshot: dict[str, Any] | None = None  # type: ignore[assignment]
        self.last_analysis: str | None = None
        self.input_mode: str = "hijack"
        self.lifecycle_state: str = "stopped"
        self._deleted_at: float | None = None  # type: ignore[assignment]
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
