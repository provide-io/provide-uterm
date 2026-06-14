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
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_tracer

try:
    from provide.uterm.cloudflare.bridge.hijack import HijackCoordinator
    from provide.uterm.cloudflare.cf_types import CFWebSocket, DurableObject
    from provide.uterm.cloudflare.config import CloudflareConfig
    from provide.uterm.cloudflare.state.store import SqliteStateStore
except Exception:  # pragma: no cover
    from bridge.hijack import (  # ty:ignore[unresolved-import]
        HijackCoordinator,  # type: ignore[import-not-found,no-redef]
    )
    from cf_types import (  # type: ignore[import-not-found,no-redef]  # ty:ignore[unresolved-import]
        CFWebSocket,
        DurableObject,
    )
    from config import CloudflareConfig  # type: ignore[import-not-found,no-redef]  # ty:ignore[unresolved-import]
    from state.store import SqliteStateStore  # type: ignore[import-not-found,no-redef]  # ty:ignore[unresolved-import]

from .auth import _AuthMixin
from .fetch import _FetchMixin
from .flow_control import FlowController
from .io import _SessionRuntimeIoMixin
from .lifecycle import _LifecycleMixin
from .ws_helpers import _WsHelperMixin

if TYPE_CHECKING:
    import asyncio

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)

# Tunnel token hashes are re-read from KV on this TTL so a revoked/rotated
# token stops working within the window and the hashes survive DO hibernation
# (where _restore_state sets _meta_loaded=True from SQLite without them).
_CREDENTIAL_TTL_S = 60.0


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
        # Tier-A backpressure controller (ACK-driven; workerd has no bufferedAmount).
        self._flow = FlowController(
            high_water=self.config.limits.backpressure_high_water_bytes,
            low_water=self.config.limits.backpressure_low_water_bytes,
            ack_grace_s=self.config.limits.backpressure_ack_grace_s,
        )
        # In-flight webhook-delivery tasks (delivery is offloaded off the broadcast
        # critical path; the set holds references so tasks aren't GC'd mid-flight).
        self._webhook_tasks: set[asyncio.Task[None]] = set()
        self.last_snapshot: dict[str, Any] | None = None
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
        # BLAKE2b digests of the tunnel-issued bearer tokens. The plain
        # values exist only in the JSON response of the create/rotate API;
        # the DO never sees them in cleartext. See provide.uterm.tunnel.
        # token_hash for the verify_token helper used at auth sites.
        self._tunnel_worker_token_hash: str | None = None
        self._share_token_hash: str | None = None
        self._control_token_hash: str | None = None
        self._issued_ip: str | None = None
        # Issue-time expiry of the tunnel's share/control tokens (epoch seconds,
        # = issuance/rotation time + the per-tunnel ttl_s). Enforced in the DO's
        # share-cookie auth path so a cookie stops authorizing WS/fetch traffic
        # once the lifetime elapses, mirroring resolve_share_context's HTTP gate.
        self._session_expires_at: float | None = None
        # Timestamp of the last _ensure_credentials() KV read (None = not loaded).
        # Independent of _meta_loaded so revocation and post-hibernation recovery work.
        self._credentials_loaded_at: float | None = None

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
                self._tunnel_worker_token_hash = str(data.get("worker_token_hash") or "") or None
                self._share_token_hash = str(data.get("share_token_hash") or "") or None
                self._control_token_hash = str(data.get("control_token_hash") or "") or None
                self._issued_ip = str(data.get("issued_ip") or "") or None
                exp = data.get("expires_at")
                self._session_expires_at = float(exp) if isinstance(exp, (int, float)) else None
        except Exception:
            logger.debug("_ensure_meta kv read failed for %s", self.worker_id)
        self.store.save_session_meta(self.worker_id, self.meta)

    async def _ensure_credentials(self) -> None:
        """Refresh tunnel token hashes from KV on a short TTL.

        Unlike _ensure_meta (one-time, gated by _meta_loaded), this always
        re-reads the credential fields from KV so a revoked/rotated token takes
        effect within _CREDENTIAL_TTL_S, and the hashes are restored after DO
        hibernation. KV is authoritative for *present* entries: the Default
        Worker writes a present-but-nulled entry to revoke/rotate, and that
        explicit null path nulls the in-memory hashes.

        A *transiently missing* entry (``raw is None``) is NOT treated as a
        revocation — it leaves the last-known hashes intact. Otherwise a brief
        KV miss (eventual consistency / not-yet-propagated write) would cause up
        to _CREDENTIAL_TTL_S of false revocation, breaking tunnel/share/control
        auth for a live session.
        """
        now = time.time()
        if self._credentials_loaded_at is not None and (now - self._credentials_loaded_at) < _CREDENTIAL_TTL_S:
            return
        kv = getattr(self.env, "SESSION_REGISTRY", None)
        if kv is None:
            self._credentials_loaded_at = now
            return
        try:
            raw = await kv.get(f"session:{self.worker_id}")
            if raw is not None:
                # Key present → authoritative. A real revoke writes the entry
                # with nulled hash fields, so these resolve to None correctly.
                data = json.loads(str(raw))
                self._tunnel_worker_token_hash = str(data.get("worker_token_hash") or "") or None
                self._share_token_hash = str(data.get("share_token_hash") or "") or None
                self._control_token_hash = str(data.get("control_token_hash") or "") or None
                self._issued_ip = str(data.get("issued_ip") or "") or None
                exp = data.get("expires_at")
                self._session_expires_at = float(exp) if isinstance(exp, (int, float)) else None
            # else: key absent → transient miss, keep the last-known hashes.
            self._credentials_loaded_at = now
        except Exception:
            logger.debug("_ensure_credentials kv read failed for %s", self.worker_id)
            # Back off for the TTL on a KV error instead of re-hitting KV on
            # every auth check while it is down; the last-known hashes stay in
            # effect (same posture as a transient miss).
            self._credentials_loaded_at = now
