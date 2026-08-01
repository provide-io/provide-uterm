#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Pydantic models and internal dataclasses for the terminal hijack hub."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from provide.uterm.bridge.coordinator import HijackSession as HijackSession  # noqa: TC001 — runtime re-export
from provide.uterm.server.bridge.rest_helpers import MAX_EXPECT_REGEX_LEN

if TYPE_CHECKING:
    from provide.uterm.bridge.contracts import InputMode
    from provide.uterm.server.gui_session import GraphicalSession


def _safe_int(val: Any, default: int, *, min_val: int | None = None) -> int:
    """Coerce *val* to ``int``, returning *default* on failure, ``None``, or out-of-range."""
    try:
        result = int(default if val is None else val)
    except (ValueError, TypeError):
        return default
    if min_val is not None and result < min_val:
        return default
    return result


def _safe_float(val: Any, default: float) -> float:
    """Coerce *val* to ``float``, returning *default* on failure or ``None``."""
    try:
        return float(default if val is None else val)
    except (ValueError, TypeError):
        return default


try:
    from fastapi import WebSocket  # noqa: TC002
    from pydantic import BaseModel, Field
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for hijack hub/routes: pip install 'provide-uterm[websocket]'") from _e


# ---------------------------------------------------------------------------
# Internal state (dataclasses — no serialisation overhead needed)
# ---------------------------------------------------------------------------

VALID_ROLES = frozenset({"viewer", "operator", "admin"})


@dataclass
class HijackLease:
    """View object for the three hijack fields on :class:`WorkerTermState`.

    Hijack ownership has two independent paths into the same lease slot:

    1. **Dashboard WS lease.** A browser holds an active hijack via its
       WebSocket. Tracked by ``ws`` + ``ws_expires_at`` (monotonic time).
       Refreshed by :meth:`touch_hijack_owner`; released when the browser
       disconnects, asks to release, or the lease expires.

    2. **REST session lease.** A non-browser client (CLI, an automation
       script, MCP) holds a hijack via the REST API. Tracked by
       ``session`` (a :class:`HijackSession`). Refreshed by the heartbeat
       endpoint; released on explicit release or lease expiry.

    Only ONE path is active per worker at any time. The dispatch order
    in :class:`~provide.uterm.server.bridge.hub.hub.HubApprovalFlowMixin` is:

        ``ws lease > REST lease > input_mode``

    so a fresh dashboard hijack supersedes a stale REST one. The
    invariant is enforced by the hub's ``_lock`` ; callers should treat
    the fields below as **read-only outside the lock**.

    This is a *view* — it borrows the three slots from a ``WorkerTermState``
    rather than owning them, so existing direct-field consumers keep
    working during migration. Methods take ``now`` explicitly to keep
    them deterministic in tests.
    """

    ws: WebSocket | None = None
    ws_expires_at: float | None = None
    session: HijackSession | None = None

    @property
    def is_idle(self) -> bool:
        return self.ws is None and self.session is None

    def is_dashboard_active(self, now: float) -> bool:
        if self.ws is None or self.ws_expires_at is None:
            return False
        return self.ws_expires_at > now

    def is_rest_active(self, now: float) -> bool:
        if self.session is None:
            return False
        return self.session.lease_expires_at > now

    def is_active(self, now: float) -> bool:
        return self.is_dashboard_active(now) or self.is_rest_active(now)

    def expire(self, now: float) -> tuple[bool, bool]:
        """Clear expired sub-leases. Returns ``(rest_expired, dash_expired)``.

        A sub-lease is considered expired when its expiry timestamp is in
        the past *and* the slot is occupied. Idle slots return False —
        clearing nothing isn't an "expiry" event for telemetry.
        """
        rest_expired = self.session is not None and self.session.lease_expires_at <= now
        dash_expired = self.ws is not None and self.ws_expires_at is not None and self.ws_expires_at <= now
        if rest_expired:
            self.session = None
        if dash_expired:
            self.ws = None
            self.ws_expires_at = None
        return rest_expired, dash_expired


@dataclass
class WorkerTermState:
    """Per-worker connection state held by :class:`~provide.uterm.server.bridge.hub.TermHub`.

    Hijack-ownership fields (``hijack_owner``, ``hijack_owner_expires_at``,
    ``hijack_session``) are kept as direct attributes for backward
    compatibility with existing consumers in
    :mod:`provide.uterm.server.bridge.hub`. New code should prefer the
    :attr:`lease` view, which provides state-machine semantics via
    :class:`HijackLease` and is documented as a unit.
    """

    worker_ws: WebSocket | None = None
    browsers: dict[WebSocket, str] = field(default_factory=dict)  # ws → role
    hijack_owner: WebSocket | None = None  # dashboard WS that holds the lease
    hijack_owner_expires_at: float | None = None
    hijack_session: HijackSession | None = None  # REST lease
    # Per-worker ownership fence. Worker-bound input holds this across the
    # network send; lease transitions take the same fence before mutating
    # ownership. The global hub lock is never held during worker I/O.
    owned_input_fence: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Monotonic token for the exact ownership epoch. Held approvals capture it
    # so release/reacquire by the same browser cannot revive stale input.
    ownership_generation: int = 0
    # Transient REST-acquire reservation. Set under the hub lock while a REST
    # acquire pauses the worker OUTSIDE the lock, then cleared when the lease is
    # finalised (or rolled back). Makes the acquire mutually exclusive without
    # holding the hub lock across the worker-pause send. See
    # ``HijackLeaseManager.try_acquire_rest``.
    hijack_pending: str | None = None
    input_mode: InputMode = "hijack"
    # Whether an authenticated caller has explicitly decided this session's
    # input mode, as opposed to it merely holding the default above.
    #
    # This exists to tell two claims apart. A `worker_hello` announces what the
    # worker process booted with; `set_input_mode` is a decision made through an
    # authenticated route by somebody holding `session.control.mode`. Without
    # the distinction the hub cannot refuse a hello that lowers `hijack` to
    # `open`, because `input_mode` defaults to `hijack` and refusing every
    # lowering would refuse every worker that legitimately announces `open`.
    #
    # It lives on the worker state rather than on the connection deliberately:
    # registry state outlives a worker socket, so a decision is not undone by a
    # reconnect. Internal only — nothing serialises it onto the wire.
    #
    # It tracks the decision, not the decider: it is cleared as soon as a
    # `worker_hello` moves `input_mode` off the decided value, because from
    # then on the mode is the worker's and refusing the worker's own way back
    # would strand the session. See ``HubConnectionService.set_worker_hello``.
    input_mode_set_by_operator: bool = False
    last_snapshot: dict[str, Any] | None = None
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=2000))
    event_seq: int = 0
    min_event_seq: int = 0
    last_activity_at: float = field(default_factory=time.monotonic)
    # Negotiated protocol version (set after worker_hello). ``None`` until
    # the handshake completes; thereafter the agreed-upon version. See
    # :func:`provide.uterm.bridge.contracts.negotiate_protocol_version`.
    protocol_version: int | None = None
    # ``True`` for workers connected via the binary-framed tunnel WS
    # (``/tunnel/{id}``). The send path uses ``ws.send_bytes`` with raw
    # PTY-bound bytes for ``input`` messages instead of the default
    # ``ws.send_text`` with a DLE-framed JSON envelope. HTTP inspect
    # controls use the tunnel ``CHANNEL_HTTP`` side-channel; other
    # non-input messages are dropped because the existing ``uterm share``
    # bridge loop writes PTY data directly. See ``hub.send_worker``.
    is_tunnel_worker: bool = False
    # Attached graphical console session (set by ``POST /worker/{id}/gui/attach``).
    # ``None`` until a graphical target is attached; thereafter the live
    # :class:`~provide.uterm.server.gui_session.GraphicalSession` used by the
    # ``/gui/`` screenshot + input routes. Mirrors the C# canonical's
    # ``WorkerTermState.GraphicalSession``.
    graphical_session: GraphicalSession | None = None

    @property
    def lease(self) -> HijackLease:
        """Construct a :class:`HijackLease` view over this state's hijack fields.

        The returned lease is a fresh object each call — mutations to the
        returned ``HijackLease`` do NOT propagate back into the underlying
        state. Use ``apply_lease`` to write changes back. Read-only
        callers (predicates, expiry checks) can use it directly.
        """
        return HijackLease(
            ws=self.hijack_owner,
            ws_expires_at=self.hijack_owner_expires_at,
            session=self.hijack_session,
        )

    def apply_lease(self, lease: HijackLease) -> None:
        """Write ``lease``'s view back onto this state's hijack fields."""
        self.hijack_owner = lease.ws
        self.hijack_owner_expires_at = lease.ws_expires_at
        self.hijack_session = lease.session


# ---------------------------------------------------------------------------
# API request models (pydantic — used by FastAPI for request validation)
# ---------------------------------------------------------------------------


class HijackAcquireRequest(BaseModel):
    owner: str = Field("operator", min_length=1, max_length=200)
    lease_s: int = Field(90, ge=1, le=14400)


class HijackHeartbeatRequest(BaseModel):
    lease_s: int = Field(90, ge=1, le=14400)


class InputModeRequest(BaseModel):
    input_mode: str = Field(..., pattern=r"^(hijack|open)$")


class HijackSendRequest(BaseModel):
    keys: str = Field(..., max_length=10_000)
    expect_prompt_id: str | None = Field(None, max_length=200)
    expect_regex: str | None = Field(None, max_length=MAX_EXPECT_REGEX_LEN)
    timeout_ms: int = Field(2000, ge=100, le=30_000)
    poll_interval_ms: int = Field(120, ge=50, le=5_000)
