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

The ``owner`` field in :class:`~provide.terminal.bridge.models.HijackAcquireRequest`
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
    raise ImportError("fastapi is required for hijack routes: pip install 'provide-terminal[websocket]'") from _e


from provide.telemetry import get_logger
from provide.terminal.bridge.contracts import (
    HijackAcquireResponse,
    HijackEventsResponse,
    HijackHeartbeatResponse,
    HijackReleaseResponse,
    HijackSnapshotResponse,
    HijackStepResponse,
    SessionModeResponse,
)
from provide.terminal.bridge.models import (
    HijackAcquireRequest,
    HijackHeartbeatRequest,
    HijackSendRequest,
    InputModeRequest,
)
from provide.terminal.bridge.rest_helpers import (
    build_hijack_events_response,
    build_hijack_snapshot_response,
    extract_prompt_id,
)


def _mono_to_wall(mono_ts: float) -> float:
    """Convert a monotonic timestamp to wall-clock for external API responses."""
    return time.time() + (mono_ts - time.monotonic())


if TYPE_CHECKING:
    from provide.terminal.bridge.hub import TermHub

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
            logger.warning("rest_acquire_rate_limited client=%s worker_id=%s", _client_id, worker_id)
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
            if not acquired:
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
                    await hub.send_worker(
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
                    "no_worker": "No worker connected.",
                    "already_hijacked": "Worker is already hijacked.",
                    "open_mode": "Hijack not available in open input mode.",
                }
                return JSONResponse({"error": error_msgs.get(err or "", str(err))}, status_code=409)
            session_committed = True
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
            return {
                "ok": True,
                "worker_id": worker_id,
                "hijack_id": hijack_id,
                "lease_expires_at": wall_now + lease_s,
                "owner": request.owner,
            }
        finally:
            if not session_committed:
                # Pause was sent but the session was never committed (e.g. client
                # disconnected and the request was cancelled).  Send a resume so
                # the worker exits the paused state.
                try:
                    await hub.send_worker(
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
        new_expires = await hub.extend_hijack_lease(worker_id, hijack_id, lease_s, now)
        if new_expires is None:  # pragma: no cover
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
        snapshot = await hub.wait_for_snapshot(worker_id, timeout_ms=wait_ms)
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
            logger.warning(
                "rest_send_rate_limited worker_id=%s hijack_id=%s client=%s", worker_id, hijack_id, _client_id
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
        # Re-validate: session may have expired (or been replaced) during the
        # wait_for_guard poll window.  A concurrent acquire could have written a
        # new HijackSession; we must confirm *this* hijack_id is still active
        # before sending keystrokes on its behalf.
        _still_valid = await hub.check_hijack_valid(worker_id, hijack_id)
        if not _still_valid:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        # Narrow race: a concurrent hijack_release could fire between the lock
        # release above and _send_worker below, unpausing the worker before these
        # keystrokes are sent.  Holding the lock across a network send is worse
        # (deadlock risk), so this sub-millisecond window is accepted.  The worker
        # processes stray keystrokes as normal input — no lock-state corruption.
        ok = await hub.send_worker(worker_id, {"type": "input", "data": request.keys, "ts": time.time()})
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
            logger.warning(
                "rest_step_rate_limited worker_id=%s hijack_id=%s client=%s", worker_id, hijack_id, _client_id
            )
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        hs = await hub.get_rest_session(worker_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        # Re-validate: session may have expired (or been replaced) during any
        # concurrent heartbeat / release / expiry cleanup since get_rest_session
        # returned (mirrors hijack_send re-validation).
        _still_valid = await hub.check_hijack_valid(worker_id, hijack_id)
        if not _still_valid:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        ok = await hub.send_worker(
            worker_id, {"type": "control", "action": "step", "owner": hs.owner, "lease_s": 0, "ts": time.time()}
        )
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
        worker_id: str = Path(pattern=r"^[\w\-]+$"), hijack_id: str = Path(pattern=r"^[0-9a-f\-]{1,64}$")
    ) -> Any:
        hs = await hub.get_rest_session(worker_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        released, should_resume = await hub.release_rest_hijack(worker_id, hijack_id)
        if not released:  # pragma: no cover
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        if should_resume and await hub.check_still_hijacked(worker_id):
            # Re-check under lock: a concurrent hijack_acquire may have written a
            # new session between release_rest_hijack and _send_worker below.
            should_resume = False
        if should_resume:
            await hub.send_worker(
                worker_id, {"type": "control", "action": "resume", "owner": hs.owner, "lease_s": 0, "ts": time.time()}
            )
            hub.notify_hijack_changed(worker_id, enabled=False, owner=None)
        hub.metric("hijack_releases_total")
        logger.info("rest_release_ok worker_id=%s hijack_id=%s owner=%s", worker_id, hijack_id, hs.owner)
        await hub.append_event(worker_id, "hijack_released", {"hijack_id": hijack_id, "owner": hs.owner})
        await hub.broadcast_hijack_state(worker_id)
        await hub.prune_if_idle(worker_id)
        return {"ok": True, "worker_id": worker_id, "hijack_id": hijack_id}

    @router.post("/worker/{worker_id}/input_mode")
    async def set_input_mode(
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        request: InputModeRequest = Body(...),  # noqa: B008
    ) -> Any:
        ok, err = await hub.set_input_mode(worker_id, request.input_mode)
        if not ok:
            status = 404 if err == "not_found" else 409
            error_msg = (
                "No worker registered." if err == "not_found" else "Cannot switch to open while hijack is active."
            )
            logger.warning("rest_input_mode_error worker_id=%s mode=%s err=%s", worker_id, request.input_mode, err)
            return JSONResponse({"error": error_msg}, status_code=status)
        logger.info("rest_input_mode_ok worker_id=%s mode=%s", worker_id, request.input_mode)
        return {"ok": True, "input_mode": request.input_mode, "worker_id": worker_id}

    @router.post("/worker/{worker_id}/disconnect_worker")
    async def disconnect_worker(
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
    ) -> Any:
        ok = await hub.disconnect_worker(worker_id)
        if not ok:
            logger.warning("rest_disconnect_no_worker worker_id=%s", worker_id)
            return JSONResponse({"error": "No worker connected."}, status_code=404)
        logger.info("rest_disconnect_ok worker_id=%s", worker_id)
        return {"ok": True, "worker_id": worker_id}
