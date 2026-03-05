#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
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

* Mount the router behind a FastAPI dependency that validates an API key or
  session token::

      from fastapi import Depends, HTTPException, Security
      from fastapi.security import HTTPBearer

      token_scheme = HTTPBearer()

      def require_token(token=Security(token_scheme)):
          if token.credentials != MY_SECRET:
              raise HTTPException(status_code=401)

      app.include_router(hub.create_router(), dependencies=[Depends(require_token)])

* Place the service behind a reverse proxy (nginx, Caddy, Traefik) that
  enforces mutual TLS or an ``Authorization`` header check.

* Bind only to localhost and restrict access via network policy when the
  hijack clients run on the same host.

The ``owner`` field in :class:`~provide.terminal.hijack.models.HijackAcquireRequest`
is an **opaque display label** — it is recorded in the event log and broadcast
to dashboard observers, but it is *not* verified.  Do not rely on it for access
control.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from typing import TYPE_CHECKING, Any

try:
    from fastapi import APIRouter, Body, Path, Query
    from fastapi.responses import JSONResponse
except ImportError as _e:  # pragma: no cover
    raise ImportError("fastapi is required for hijack routes: pip install 'provide-terminal[websocket]'") from _e

import logging

from provide.terminal.hijack.models import (
    HijackAcquireRequest,
    HijackHeartbeatRequest,
    HijackSendRequest,
    InputModeRequest,
    extract_prompt_id,
)

if TYPE_CHECKING:
    from provide.terminal.hijack.hub import TermHub

logger = logging.getLogger(__name__)


def register_rest_routes(hub: TermHub, router: APIRouter) -> None:
    """Attach REST hijack routes to *router*.

    .. warning::
        No authentication is applied.  Callers are responsible for protecting
        the router before exposing it to untrusted clients — see the module
        docstring for guidance.
    """

    @router.post("/worker/{worker_id}/hijack/acquire")
    async def hijack_acquire(
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        request: HijackAcquireRequest | None = None,
    ) -> Any:
        if request is None:
            request = HijackAcquireRequest()  # type: ignore[call-arg]
        await hub._cleanup_expired_hijack(worker_id)

        # No pre-flight worker check here — _send_worker is the authoritative
        # liveness gate. A pre-check via _get() releases the lock immediately,
        # so a worker connecting between the check and _send_worker would be
        # incorrectly rejected with 409. _send_worker handles the None case and
        # returns False, which is caught at the ok check below.
        lease_s = hub._clamp_lease(request.lease_s)
        hijack_id = str(uuid.uuid4())
        now = time.time()
        ok = await hub._send_worker(
            worker_id,
            {
                "type": "control",
                "action": "pause",
                "owner": request.owner,
                "lease_s": lease_s,
                "hijack_id": hijack_id,
                "ts": now,
            },
        )
        if not ok:
            return JSONResponse({"error": "No worker connected for this worker."}, status_code=409)

        # From here the worker is paused. Guard against CancelledError (client
        # disconnect) or any other exception raised before the session is
        # committed: the finally block sends a compensating resume so the worker
        # is not permanently stuck in the paused state.
        session_committed = False
        try:
            # Atomically check for concurrent hijackers and write the session.
            acquired, err = await hub._try_acquire_rest_hijack(
                worker_id,
                owner=request.owner,
                lease_s=lease_s,
                hijack_id=hijack_id,
                now=now,
            )
            if not acquired:
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
                    await hub._send_worker(
                        worker_id,
                        {
                            "type": "control",
                            "action": "resume",
                            "owner": request.owner,
                            "lease_s": 0,
                            "hijack_id": hijack_id,
                            "ts": now,
                        },
                    )
                error_msg = "No worker connected." if err == "no_worker" else "Worker is already hijacked."
                return JSONResponse({"error": error_msg}, status_code=409)
            session_committed = True
            hub._notify_hijack_changed(worker_id, enabled=True, owner=request.owner)
            await hub._append_event(
                worker_id, "hijack_acquired", {"hijack_id": hijack_id, "owner": request.owner, "lease_s": lease_s}
            )
            await hub._broadcast_hijack_state(worker_id)
            return {
                "ok": True,
                "worker_id": worker_id,
                "hijack_id": hijack_id,
                "lease_expires_at": now + lease_s,
                "owner": request.owner,
            }
        finally:
            if not session_committed:
                # Pause was sent but the session was never committed (e.g. client
                # disconnected and the request was cancelled).  Send a resume so
                # the worker exits the paused state.
                with contextlib.suppress(Exception):
                    await hub._send_worker(
                        worker_id,
                        {
                            "type": "control",
                            "action": "resume",
                            "owner": request.owner,
                            "lease_s": 0,
                            "hijack_id": hijack_id,
                            "ts": now,
                        },
                    )

    @router.post("/worker/{worker_id}/hijack/{hijack_id}/heartbeat")
    async def hijack_heartbeat(
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(pattern=r"^[0-9a-f\-]{1,64}$"),
        request: HijackHeartbeatRequest | None = None,
    ) -> Any:
        if request is None:
            request = HijackHeartbeatRequest()  # type: ignore[call-arg]
        hs = await hub._get_rest_session(worker_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        lease_s = hub._clamp_lease(request.lease_s)
        now = time.time()
        new_expires: float
        async with hub._lock:
            st = hub._workers.get(worker_id)
            if st is None or st.hijack_session is None or st.hijack_session.hijack_id != hijack_id:  # pragma: no cover
                return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
            st.hijack_session.last_heartbeat = now
            st.hijack_session.lease_expires_at = now + lease_s
            new_expires = st.hijack_session.lease_expires_at
        await hub._append_event(worker_id, "hijack_heartbeat", {"hijack_id": hijack_id, "lease_s": lease_s})
        await hub._broadcast_hijack_state(worker_id)
        return {"ok": True, "hijack_id": hijack_id, "lease_expires_at": new_expires}

    @router.get("/worker/{worker_id}/hijack/{hijack_id}/snapshot")
    async def hijack_snapshot(
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(pattern=r"^[0-9a-f\-]{1,64}$"),
        wait_ms: int = Query(default=1500, ge=50, le=10000),
    ) -> Any:
        hs = await hub._get_rest_session(worker_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        snapshot = await hub._wait_for_snapshot(worker_id, timeout_ms=wait_ms)
        # Re-read lease_expires_at under the lock: a concurrent heartbeat may
        # have extended it during the _wait_for_snapshot poll loop.
        async with hub._lock:
            st = hub._workers.get(worker_id)
            fresh_expires = (
                st.hijack_session.lease_expires_at
                if st is not None and st.hijack_session is not None and st.hijack_session.hijack_id == hijack_id
                else hs.lease_expires_at
            )
        return {
            "ok": True,
            "worker_id": worker_id,
            "hijack_id": hijack_id,
            "snapshot": snapshot,
            "prompt_id": extract_prompt_id(snapshot),
            "lease_expires_at": fresh_expires,
        }

    @router.get("/worker/{worker_id}/hijack/{hijack_id}/events")
    async def hijack_events(
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(pattern=r"^[0-9a-f\-]{1,64}$"),
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=2000),
    ) -> Any:
        hs = await hub._get_rest_session(worker_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        async with hub._lock:
            st = hub._workers.get(worker_id)
            if st is None:  # pragma: no cover
                rows: list[dict[str, Any]] = []
                latest_seq = 0
                fresh_expires = hs.lease_expires_at
            else:
                rows = [evt for evt in list(st.events) if int(evt.get("seq", 0)) > after_seq][:limit]
                latest_seq = st.event_seq
                # Re-read under lock: a concurrent heartbeat may have extended
                # the lease since _get_rest_session returned.
                fresh_expires = (
                    st.hijack_session.lease_expires_at
                    if st.hijack_session is not None and st.hijack_session.hijack_id == hijack_id
                    else hs.lease_expires_at
                )
        return {
            "ok": True,
            "worker_id": worker_id,
            "hijack_id": hijack_id,
            "after_seq": after_seq,
            "latest_seq": latest_seq,
            "events": rows,
            "lease_expires_at": fresh_expires,
        }

    @router.post("/worker/{worker_id}/hijack/{hijack_id}/send")
    async def hijack_send(
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(pattern=r"^[0-9a-f\-]{1,64}$"),
        request: HijackSendRequest = Body(...),  # noqa: B008
    ) -> Any:
        hs = await hub._get_rest_session(worker_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        if not request.keys:
            return JSONResponse({"error": "keys must not be empty."}, status_code=400)
        matched, snapshot, reason = await hub._wait_for_guard(
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
        # _wait_for_guard poll window.  A concurrent acquire could have written a
        # new HijackSession; we must confirm *this* hijack_id is still active
        # before sending keystrokes on its behalf.
        async with hub._lock:
            _st = hub._workers.get(worker_id)
            _still_valid = (
                _st is not None
                and _st.hijack_session is not None
                and _st.hijack_session.hijack_id == hijack_id
                and _st.hijack_session.lease_expires_at > time.time()
            )
        if not _still_valid:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        # Narrow race: a concurrent hijack_release could fire between the lock
        # release above and _send_worker below, unpausing the worker before these
        # keystrokes are sent.  Holding the lock across a network send is worse
        # (deadlock risk), so this sub-millisecond window is accepted.  The worker
        # processes stray keystrokes as normal input — no lock-state corruption.
        ok = await hub._send_worker(worker_id, {"type": "input", "data": request.keys, "ts": time.time()})
        if not ok:
            return JSONResponse({"error": "No worker connected for this worker."}, status_code=409)
        await hub._append_event(
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
        # have extended it during the _wait_for_guard poll (mirrors hijack_snapshot).
        async with hub._lock:
            st = hub._workers.get(worker_id)
            fresh_expires = (
                st.hijack_session.lease_expires_at
                if st is not None and st.hijack_session is not None and st.hijack_session.hijack_id == hijack_id
                else hs.lease_expires_at
            )
        return {
            "ok": True,
            "worker_id": worker_id,
            "hijack_id": hijack_id,
            "sent": request.keys,
            "matched_prompt_id": extract_prompt_id(snapshot),
            "lease_expires_at": fresh_expires,
        }

    @router.post("/worker/{worker_id}/hijack/{hijack_id}/step")
    async def hijack_step(
        worker_id: str = Path(pattern=r"^[\w\-]+$"), hijack_id: str = Path(pattern=r"^[0-9a-f\-]{1,64}$")
    ) -> Any:
        hs = await hub._get_rest_session(worker_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        # Re-validate: session may have expired (or been replaced) during any
        # concurrent heartbeat / release / expiry cleanup since _get_rest_session
        # returned (mirrors hijack_send re-validation).
        async with hub._lock:
            _st = hub._workers.get(worker_id)
            _still_valid = (
                _st is not None
                and _st.hijack_session is not None
                and _st.hijack_session.hijack_id == hijack_id
                and _st.hijack_session.lease_expires_at > time.time()
            )
        if not _still_valid:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        ok = await hub._send_worker(
            worker_id, {"type": "control", "action": "step", "owner": hs.owner, "lease_s": 0, "ts": time.time()}
        )
        if not ok:
            return JSONResponse({"error": "No worker connected for this worker."}, status_code=409)
        await hub._append_event(worker_id, "hijack_step", {"hijack_id": hijack_id})
        async with hub._lock:
            st = hub._workers.get(worker_id)
            fresh_expires = (
                st.hijack_session.lease_expires_at
                if st is not None and st.hijack_session is not None and st.hijack_session.hijack_id == hijack_id
                else hs.lease_expires_at
            )
        return {"ok": True, "worker_id": worker_id, "hijack_id": hijack_id, "lease_expires_at": fresh_expires}

    @router.post("/worker/{worker_id}/hijack/{hijack_id}/release")
    async def hijack_release(
        worker_id: str = Path(pattern=r"^[\w\-]+$"), hijack_id: str = Path(pattern=r"^[0-9a-f\-]{1,64}$")
    ) -> Any:
        hs = await hub._get_rest_session(worker_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        should_resume = False
        async with hub._lock:
            st = hub._workers.get(worker_id)
            if st is None or st.hijack_session is None or st.hijack_session.hijack_id != hijack_id:  # pragma: no cover
                return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
            st.hijack_session = None
            should_resume = not hub._is_dashboard_hijack_active(st)
        if should_resume:
            # Re-check under lock: a concurrent hijack_acquire may have written a
            # new session between the lock release above and _send_worker below.
            async with hub._lock:
                _st2 = hub._workers.get(worker_id)
                if _st2 is not None and hub._is_hijacked(_st2):
                    should_resume = False
        if should_resume:
            await hub._send_worker(
                worker_id, {"type": "control", "action": "resume", "owner": hs.owner, "lease_s": 0, "ts": time.time()}
            )
            hub._notify_hijack_changed(worker_id, enabled=False, owner=None)
        await hub._append_event(worker_id, "hijack_released", {"hijack_id": hijack_id, "owner": hs.owner})
        await hub._broadcast_hijack_state(worker_id)
        await hub._prune_if_idle(worker_id)
        return {"ok": True, "worker_id": worker_id, "hijack_id": hijack_id}

    @router.post("/worker/{worker_id}/input_mode")
    async def set_input_mode(
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
        request: InputModeRequest = Body(...),  # noqa: B008
    ) -> Any:
        ok, err = await hub._set_input_mode(worker_id, request.input_mode)
        if not ok:
            status = 404 if err == "not_found" else 409
            error_msg = "No worker registered." if err == "not_found" else "Cannot switch to open while hijack is active."
            return JSONResponse({"error": error_msg}, status_code=status)
        return {"ok": True, "input_mode": request.input_mode, "worker_id": worker_id}

    @router.post("/worker/{worker_id}/disconnect_worker")
    async def disconnect_worker(
        worker_id: str = Path(pattern=r"^[\w\-]+$"),
    ) -> Any:
        ok = await hub._disconnect_worker(worker_id)
        if not ok:
            return JSONResponse({"error": "No worker connected."}, status_code=404)
        return {"ok": True, "worker_id": worker_id}
