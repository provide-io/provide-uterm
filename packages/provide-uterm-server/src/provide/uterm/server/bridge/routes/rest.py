#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""REST hijack routes for the hijack hub.

Registers:
- ``POST /worker/{id}/hijack/acquire``
- ``POST /worker/{id}/hijack/{hid}/heartbeat``
- ``GET  /worker/{id}/hijack/{hid}/snapshot``
- ``GET  /worker/{id}/hijack/{hid}/events``
- ``POST /worker/{id}/hijack/{hid}/send``
- ``POST /worker/{id}/hijack/{hid}/step``
- ``POST /worker/{id}/hijack/{hid}/release``

.. rubric:: Authentication

These routes have **no built-in authentication or authorisation**.  Any caller
that can reach the router can acquire a hijack lease and send keystrokes to any
worker.  You *must* protect the router at the application layer before exposing it
to untrusted clients.  Typical approaches:

.. rubric:: CSRF

These endpoints are designed to be called by server-side agents or API clients
using an ``Authorization: Bearer <token>`` header.  If you expose them to
browser-based callers that authenticate via session cookies you **must** add
CSRF protection at the application layer (e.g. a double-submit cookie, a
synchroniser token, or ``SameSite=Strict`` on session cookies).

* Mount the router behind a FastAPI dependency that validates an API key or
  session token::

      from fastapi import Depends, HTTPException, Security
      from fastapi.security import HTTPBearer
from provide.telemetry import get_logger

      token_scheme = HTTPBearer()

      def require_token(token=Security(token_scheme)):
          if token.credentials != MY_SECRET:
              raise HTTPException(status_code=401)

      app.include_router(hub.create_router(), dependencies=[Depends(require_token)])

* Place the service behind a reverse proxy (nginx, Caddy, Traefik) that
  enforces mutual TLS or an ``Authorization`` header check.

* Bind only to localhost and restrict access via network policy when the
  hijack clients run on the same host.

The ``owner`` field in :class:`~provide.uterm.server.bridge.models.HijackAcquireRequest`
is an **opaque display label** — it is recorded in the event log and broadcast
to dashboard observers, but it is *not* verified.  Do not rely on it for access
control.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any

try:
    from fastapi import APIRouter, Body, Path, Query, Request
    from fastapi.responses import JSONResponse
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for hijack routes: pip install 'provide-uterm[websocket]'") from _e


from provide.telemetry import get_logger
from provide.uterm.server.bridge.models import (
    HijackAcquireRequest,
    HijackHeartbeatRequest,
    HijackSendRequest,
)
from provide.uterm.server.bridge.rest_helpers import (
    build_hijack_events_response,
    build_hijack_snapshot_response,
    extract_prompt_id,
)
from provide.uterm.server.bridge.routes.rest_gui import register_gui_routes
from provide.uterm.server.bridge.routes.rest_workerctl import register_workerctl_routes
from provide.uterm.server.bridge.routes.ws_gui_vnc import register_gui_vnc_ws_routes


def _mono_to_wall(mono_ts: float) -> float:
    """Convert a monotonic timestamp to wall-clock for external API responses."""
    return time.time() + (mono_ts - time.monotonic())


def _principal_subject(http_request: Request) -> str | None:
    """The authenticated principal's subject_id, or None (unauthenticated/legacy)."""
    subject = getattr(getattr(http_request.state, "uterm_principal", None), "subject_id", None)
    return str(subject) if subject is not None else None


async def _may_release_lease(http_request: Request, worker_id: str, hs: Any) -> bool:
    """REST lease-release ownership: the acquiring principal, the session owner, or a
    global admin may release. ``acquired_by is None`` (legacy/unauthenticated leases)
    keeps the prior capability model — possession of the unguessable hijack_id."""
    requester = _principal_subject(http_request)
    if hs.acquired_by is None or requester == hs.acquired_by:
        return True
    session = await http_request.app.state.uterm_registry.get_definition(worker_id)
    if session is not None and session.owner == requester:
        return True
    return bool(await http_request.app.state.uterm_authz.is_admin(http_request.state.uterm_principal))


if TYPE_CHECKING:
    from provide.uterm.server.bridge.hub import TermHub

logger = get_logger(__name__)


def register_rest_routes(hub: TermHub, router: APIRouter) -> None:
    """Attach REST hijack routes to *router*.

    .. warning::
        No authentication is applied.  Callers are responsible for protecting
        the router before exposing it to untrusted clients — see the module
        docstring for guidance.
    """

    @router.post("/worker/{worker_id}/hijack/acquire")
    async def hijack_acquire(
        http_request: Request,
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        request: HijackAcquireRequest | None = None,
    ) -> Any:
        # NOTE: Uses the direct connection IP for per-client rate limiting.
        # Behind a reverse proxy this will be 127.0.0.1, collapsing all clients
        # into one bucket.  Trusting X-Forwarded-For without a trusted-proxy
        # allowlist would be spoofable, so it is intentionally not used here.
        # Deploy a gateway that enforces per-client limits before this service
        # if fine-grained rate limiting is required.
        _client_id = (http_request.client.host if http_request.client else None) or "unknown"
        if not hub.allow_rest_acquire_for(_client_id):
            hub.metric("rest_acquire_rate_limited_total")
            logger.warning("rest_acquire_rate_limited client=%s worker_id=%s", _client_id, worker_id)
            await hub.emit_telemetry(
                "rate_limit.triggered",
                worker_id=worker_id,
                metadata={"client_id": _client_id, "limit_type": "rest_acquire"},
            )
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        if request is None:
            request = HijackAcquireRequest.model_validate({})
        await hub.cleanup_expired_hijack(worker_id)

        # No pre-flight worker check here — _send_worker is the authoritative
        # liveness gate. A pre-check via _get() releases the lock immediately,
        # so a worker connecting between the check and _send_worker would be
        # incorrectly rejected with 409. _send_worker handles the None case and
        # returns False, which is caught at the ok check below.
        lease_s = hub.clamp_lease(request.lease_s)
        hijack_id = str(uuid.uuid4())
        wall_now = time.time()
        mono_now = time.monotonic()

        # From here the worker is paused (atomically in try_acquire_rest_hijack).
        # Guard against CancelledError (client disconnect) or any other exception
        # raised before the session is committed: the finally block sends a
        # compensating resume so the worker exits the hold state.
        session_committed = False
        try:
            ok, err = await hub.try_acquire_rest_hijack(
                worker_id,
                owner=request.owner,
                lease_s=lease_s,
                hijack_id=hijack_id,
                now=mono_now,
            )
            if not ok:
                if err == "already_hijacked":
                    hub.metric("hijack_conflicts_total")
                    logger.warning(
                        "rest_acquire_conflict worker_id=%s owner=%s client=%s",
                        worker_id,
                        request.owner,
                        _client_id,
                    )
                else:
                    logger.warning(
                        "rest_acquire_no_worker worker_id=%s owner=%s client=%s",
                        worker_id,
                        request.owner,
                        _client_id,
                    )
                # session_committed=True prevents the finally block from
                # sending a second resume.  If err=="no_worker" (worker
                # disconnected between _send_worker and the lock), _send_worker
                # below is a silent no-op — there is nobody to resume, which is
                # correct.  Do NOT send resume for err=="already_hijacked":
                # set_hijacked is a boolean (not a reference count), so the
                # pause we sent was a no-op (worker already paused), and
                # sending resume here would unpause the legitimate owner's session.
                session_committed = True
                if err != "already_hijacked":
                    await hub.send_worker_if_unowned(
                        worker_id,
                        {
                            "type": "control",
                            "action": "resume",
                            "owner": request.owner,
                            "lease_s": 0,
                            "hijack_id": hijack_id,
                            "ts": wall_now,
                        },
                    )
                error_msgs = {
                    "no_worker": "No worker connected for this session.",
                    "already_hijacked": "Worker is already hijacked.",
                    "open_mode": "Hijack not available in open input mode.",
                }
                return JSONResponse({"error": error_msgs.get(err or "", str(err))}, status_code=409)
            hub.metric("hijack_acquires_total")
            logger.info(
                "rest_acquire_ok worker_id=%s hijack_id=%s owner=%s lease_s=%d client=%s",
                worker_id,
                hijack_id,
                request.owner,
                lease_s,
                _client_id,
            )
            hub.notify_hijack_changed(worker_id, enabled=True, owner=request.owner)
            await hub.append_event(
                worker_id, "hijack_acquired", {"hijack_id": hijack_id, "owner": request.owner, "lease_s": lease_s}
            )
            await hub.broadcast_hijack_state(worker_id)
            # Record the acquiring principal on the live lease so release can verify
            # ownership (the REST ``owner`` field is a self-declared display label).
            _acquired = await hub.get_rest_session(worker_id, hijack_id)
            if _acquired is not None:  # pragma: no branch - present right after commit
                _acquired.acquired_by = _principal_subject(http_request)
            session_committed = True
            return {
                "ok": True,
                "worker_id": worker_id,
                "hijack_id": hijack_id,
                "lease_expires_at": wall_now + lease_s,
                "owner": request.owner,
            }
        finally:
            if not session_committed:
                # The lease manager commits its in-memory reservation before
                # route-level observability runs. If the request is cancelled
                # after that point, roll the exact lease back before resuming;
                # otherwise a live lease would describe an unpaused worker.
                try:
                    released, _ = await hub.release_rest_hijack(worker_id, hijack_id)
                    await hub.send_worker_if_unowned(
                        worker_id,
                        {
                            "type": "control",
                            "action": "resume",
                            "owner": request.owner,
                            "lease_s": 0,
                            "hijack_id": hijack_id,
                            "ts": wall_now,
                        },
                    )
                    if released:
                        hub.notify_hijack_changed(worker_id, enabled=False, owner=None)
                        await hub.broadcast_hijack_state(worker_id)
                except (asyncio.CancelledError, OSError, RuntimeError) as exc:
                    logger.warning("hijack_acquire_compensating_resume_failed worker_id=%s: %s", worker_id, exc)

    @router.post("/worker/{worker_id}/hijack/{hijack_id}/heartbeat")
    async def hijack_heartbeat(
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(pattern=r"^[0-9a-f\-]{1,64}$"),
        request: HijackHeartbeatRequest | None = None,
    ) -> Any:
        if request is None:
            request = HijackHeartbeatRequest.model_validate({})
        hs = await hub.get_rest_session(worker_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        lease_s = hub.clamp_lease(request.lease_s)
        now = time.monotonic()
        new_expires = await hub.extend_hijack_lease(worker_id, hijack_id, hs.owner, lease_s, now)
        if new_expires is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        await hub.append_event(worker_id, "hijack_heartbeat", {"hijack_id": hijack_id, "lease_s": lease_s})
        await hub.broadcast_hijack_state(worker_id)
        return {
            "ok": True,
            "worker_id": worker_id,
            "hijack_id": hijack_id,
            "lease_expires_at": _mono_to_wall(new_expires),
        }

    @router.get("/worker/{worker_id}/hijack/{hijack_id}/snapshot")
    async def hijack_snapshot(
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(pattern=r"^[0-9a-f\-]{1,64}$"),
        wait_ms: int = Query(default=1500, ge=50, le=10000),
    ) -> Any:
        hs = await hub.get_rest_session(worker_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        # Freshness is measured against what THIS lease was last served, not
        # against the moment it asked. A worker pushing on screen change lands
        # the new frame microseconds BEFORE the poll, and the wall-clock test
        # then discarded exactly that frame — the caller timed out still holding
        # an older screen. See PollingCoordinator.wait_for_snapshot.
        snapshot = await hub.wait_for_snapshot(worker_id, timeout_ms=wait_ms, after_event_seq=hs.last_served_event_seq)
        if snapshot is not None:
            served = snapshot.get("event_seq")
            if isinstance(served, int) and not isinstance(served, bool):
                hs.last_served_event_seq = served
        # Re-read lease_expires_at under the lock: a concurrent heartbeat may
        # have extended it during the wait_for_snapshot poll loop.
        fresh_expires = await hub.get_fresh_hijack_expiry(worker_id, hijack_id, hs.lease_expires_at)
        return build_hijack_snapshot_response(
            worker_id=worker_id,
            hijack_id=hijack_id,
            snapshot=snapshot,
            lease_expires_at=_mono_to_wall(fresh_expires),
        )

    @router.get("/worker/{worker_id}/hijack/{hijack_id}/events")
    async def hijack_events(
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(pattern=r"^[0-9a-f\-]{1,64}$"),
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=2000),
    ) -> Any:
        hs = await hub.get_rest_session(worker_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        events_data = await hub.get_hijack_events_data(worker_id, hijack_id, hs, after_seq, limit)
        rows = events_data["rows"]
        latest_seq = events_data["latest_seq"]
        min_event_seq = events_data["min_event_seq"]
        fresh_expires = events_data["fresh_expires"]
        return build_hijack_events_response(
            worker_id=worker_id,
            hijack_id=hijack_id,
            after_seq=after_seq,
            latest_seq=latest_seq,
            min_event_seq=min_event_seq,
            events=rows,
            limit=limit,
            lease_expires_at=_mono_to_wall(fresh_expires),
        )

    @router.post("/worker/{worker_id}/hijack/{hijack_id}/send")
    async def hijack_send(
        http_request: Request,
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(pattern=r"^[0-9a-f\-]{1,64}$"),
        request: HijackSendRequest = Body(...),  # noqa: B008
    ) -> Any:
        _client_id = (http_request.client.host if http_request.client else None) or "unknown"
        if not hub.allow_rest_send_for(_client_id):
            hub.metric("rest_send_rate_limited_total")
            logger.warning(
                "rest_send_rate_limited worker_id=%s hijack_id=%s client=%s", worker_id, hijack_id, _client_id
            )
            await hub.emit_telemetry(
                "rate_limit.triggered",
                worker_id=worker_id,
                metadata={"client_id": _client_id, "limit_type": "rest_send"},
            )
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        hs = await hub.get_rest_session(worker_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        if not request.keys:
            return JSONResponse({"error": "keys must not be empty."}, status_code=400)
        if len(request.keys) > hub.max_input_chars:
            return JSONResponse(
                {"error": f"keys too long: {len(request.keys)} > {hub.max_input_chars}"},
                status_code=400,
            )
        matched, snapshot, reason = await hub.wait_for_guard(
            worker_id,
            expect_prompt_id=request.expect_prompt_id,
            expect_regex=request.expect_regex,
            timeout_ms=request.timeout_ms,
            poll_interval_ms=request.poll_interval_ms,
        )
        if not matched:
            return JSONResponse(
                {"error": reason or "prompt_guard_not_satisfied", "current_prompt_id": extract_prompt_id(snapshot)},
                status_code=409,
            )
        # Revalidate the exact lease only after the guard wait, then reserve
        # that ownership through delivery. Release/expiry transitions take the
        # same per-worker fence, without holding the global hub lock over I/O.
        ok, error = await hub.send_owned_worker(
            worker_id,
            {"type": "input", "data": request.keys, "ts": time.time()},
            rest_hijack_id=hijack_id,
        )
        if error == "invalid_owner":
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        if not ok:
            return JSONResponse({"error": "No worker connected for this session."}, status_code=409)
        logger.info(
            "rest_send_ok worker_id=%s hijack_id=%s client=%s keys_len=%d",
            worker_id,
            hijack_id,
            _client_id,
            len(request.keys),
        )
        await hub.append_event(
            worker_id,
            "hijack_send",
            {
                "hijack_id": hijack_id,
                "keys": request.keys[:120],
                "expect_prompt_id": request.expect_prompt_id,
                "expect_regex": request.expect_regex,
            },
        )
        # Re-read lease_expires_at under the lock: a concurrent heartbeat may
        # have extended it during the wait_for_guard poll (mirrors hijack_snapshot).
        fresh_expires = await hub.get_fresh_hijack_expiry(worker_id, hijack_id, hs.lease_expires_at)
        return {
            "ok": True,
            "worker_id": worker_id,
            "hijack_id": hijack_id,
            "sent": request.keys,
            "matched_prompt_id": extract_prompt_id(snapshot),
            "lease_expires_at": _mono_to_wall(fresh_expires),
        }

    @router.post("/worker/{worker_id}/hijack/{hijack_id}/step")
    async def hijack_step(
        http_request: Request,
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(pattern=r"^[0-9a-f\-]{1,64}$"),
    ) -> Any:
        _client_id = (http_request.client.host if http_request.client else None) or "unknown"
        if not hub.allow_rest_send_for(_client_id):
            hub.metric("rest_step_rate_limited_total")
            logger.warning(
                "rest_step_rate_limited worker_id=%s hijack_id=%s client=%s", worker_id, hijack_id, _client_id
            )
            await hub.emit_telemetry(
                "rate_limit.triggered",
                worker_id=worker_id,
                metadata={"client_id": _client_id, "limit_type": "rest_step"},
            )
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        hs = await hub.get_rest_session(worker_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        ok, error = await hub.send_owned_worker(
            worker_id,
            {"type": "control", "action": "step", "owner": hs.owner, "lease_s": 0, "ts": time.time()},
            rest_hijack_id=hijack_id,
        )
        if error == "invalid_owner":
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        if not ok:
            return JSONResponse({"error": "No worker connected for this session."}, status_code=409)
        logger.info("rest_step_ok worker_id=%s hijack_id=%s client=%s", worker_id, hijack_id, _client_id)
        await hub.append_event(worker_id, "hijack_step", {"hijack_id": hijack_id})
        hub.metric("hijack_steps_total")
        fresh_expires = await hub.get_fresh_hijack_expiry(worker_id, hijack_id, hs.lease_expires_at)
        return {
            "ok": True,
            "worker_id": worker_id,
            "hijack_id": hijack_id,
            "lease_expires_at": _mono_to_wall(fresh_expires),
        }

    @router.post("/worker/{worker_id}/hijack/{hijack_id}/release")
    async def hijack_release(
        http_request: Request,
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(pattern=r"^[0-9a-f\-]{1,64}$"),
    ) -> Any:
        hs = await hub.get_rest_session(worker_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        # Lease ownership: only the acquiring principal, the session owner, or a
        # global admin may drop an active lease — not any operator who reaches this
        # route via shared-session can_mutate_session.
        if not await _may_release_lease(http_request, worker_id, hs):
            return JSONResponse({"error": "Not the lease owner."}, status_code=403)
        released, should_resume = await hub.release_rest_hijack(worker_id, hijack_id)
        if not released:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        if should_resume and await hub.check_still_hijacked(worker_id):
            # Re-check under lock: a concurrent hijack_acquire may have written a
            # new session between release_rest_hijack and _send_worker below.
            should_resume = False
        if should_resume:
            await hub.send_worker_if_unowned(
                worker_id, {"type": "control", "action": "resume", "owner": hs.owner, "lease_s": 0, "ts": time.time()}
            )
        # Always notify subscribers (e.g. bbsbot SwarmManager's bot.is_hijacked
        # mirror) that THIS rest hijack is gone, regardless of whether a
        # ``resume`` worker-frame was sent. ``should_resume`` only gates the
        # worker-frame because a concurrent dashboard hijack or new REST
        # acquire wants the worker to stay paused — but our specific REST
        # lease IS released either way. Pre-fix, ``notify_hijack_changed``
        # sat inside the ``if should_resume`` block, leaving downstream
        # mirrors stuck on ``is_hijacked=True``. bbsbot then 409'd the next
        # ``/swarm/compare/acquire`` with "Worker is already hijacked" even
        # though the actual hub state had no REST session (caught in
        # uwarp 2026-05-24 compare-iter wedge cycles).
        hub.notify_hijack_changed(worker_id, enabled=False, owner=None)
        hub.metric("hijack_releases_total")
        logger.info("rest_release_ok worker_id=%s hijack_id=%s owner=%s", worker_id, hijack_id, hs.owner)
        await hub.append_event(worker_id, "hijack_released", {"hijack_id": hijack_id, "owner": hs.owner})
        await hub.broadcast_hijack_state(worker_id)
        await hub.prune_if_idle(worker_id)
        return {"ok": True, "worker_id": worker_id, "hijack_id": hijack_id}

    # Worker-control routes (input_mode, disconnect_worker) live in the sibling
    # ``rest_workerctl`` module; register them on the same router so the public
    # surface (a single ``register_rest_routes`` call) is unchanged.
    register_workerctl_routes(hub, router)
    # GUI (graphical console) routes live in the sibling ``rest_gui`` module;
    # register them here so ``/gui/`` screenshot + input endpoints share the
    # single ``register_rest_routes`` public surface.
    register_gui_routes(hub, router)
    # Human VNC WebSocket relay (authz + optional upstream duplex factory).
    register_gui_vnc_ws_routes(hub, router)
