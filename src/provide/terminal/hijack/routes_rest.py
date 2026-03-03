#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""REST hijack routes for the hijack hub.

Registers:
- ``POST /bot/{id}/hijack/acquire``
- ``POST /bot/{id}/hijack/{hid}/heartbeat``
- ``GET  /bot/{id}/hijack/{hid}/snapshot``
- ``GET  /bot/{id}/hijack/{hid}/events``
- ``POST /bot/{id}/hijack/{hid}/send``
- ``POST /bot/{id}/hijack/{hid}/step``
- ``POST /bot/{id}/hijack/{hid}/release``

.. rubric:: Authentication

These routes have **no built-in authentication or authorisation**.  Any caller
that can reach the router can acquire a hijack lease and send keystrokes to any
bot.  You *must* protect the router at the application layer before exposing it
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

    @router.post("/bot/{bot_id}/hijack/acquire")
    async def hijack_acquire(
        bot_id: str = Path(pattern=r"^[\w\-]+$"),
        request: HijackAcquireRequest | None = None,
    ) -> Any:
        if request is None:
            request = HijackAcquireRequest()
        await hub._cleanup_expired_hijack(bot_id)

        # No pre-flight worker check here — _send_worker is the authoritative
        # liveness gate. A pre-check via _get() releases the lock immediately,
        # so a worker connecting between the check and _send_worker would be
        # incorrectly rejected with 409. _send_worker handles the None case and
        # returns False, which is caught at the ok check below.
        lease_s = hub._clamp_lease(request.lease_s)
        hijack_id = str(uuid.uuid4())
        now = time.time()
        ok = await hub._send_worker(
            bot_id,
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
            return JSONResponse({"error": "No worker connected for this bot."}, status_code=409)

        # From here the worker is paused. Guard against CancelledError (client
        # disconnect) or any other exception raised before the session is
        # committed: the finally block sends a compensating resume so the worker
        # is not permanently stuck in the paused state.
        session_committed = False
        try:
            # Atomically check for concurrent hijackers and write the session.
            acquired, err = await hub._try_acquire_rest_hijack(
                bot_id,
                owner=request.owner,
                lease_s=lease_s,
                hijack_id=hijack_id,
                now=now,
            )
            if not acquired:
                # Another request raced in; send resume to undo our pause.
                # Set session_committed so the finally block skips a second send.
                session_committed = True
                await hub._send_worker(
                    bot_id,
                    {
                        "type": "control",
                        "action": "resume",
                        "owner": request.owner,
                        "lease_s": 0,
                        "hijack_id": hijack_id,
                        "ts": now,
                    },
                )
                return JSONResponse({"error": "Bot is already hijacked."}, status_code=409)
            session_committed = True
            hub._notify_hijack_changed(bot_id, enabled=True, owner=request.owner)
            await hub._append_event(
                bot_id, "hijack_acquired", {"hijack_id": hijack_id, "owner": request.owner, "lease_s": lease_s}
            )
            await hub._broadcast_hijack_state(bot_id)
            return {
                "ok": True,
                "bot_id": bot_id,
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
                        bot_id,
                        {
                            "type": "control",
                            "action": "resume",
                            "owner": request.owner,
                            "lease_s": 0,
                            "hijack_id": hijack_id,
                            "ts": now,
                        },
                    )

    @router.post("/bot/{bot_id}/hijack/{hijack_id}/heartbeat")
    async def hijack_heartbeat(
        bot_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(),
        request: HijackHeartbeatRequest | None = None,
    ) -> Any:
        if request is None:
            request = HijackHeartbeatRequest()
        hs = await hub._get_rest_session(bot_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        lease_s = hub._clamp_lease(request.lease_s)
        now = time.time()
        async with hub._lock:
            st = hub._bots.get(bot_id)
            if st is None or st.hijack_session is None or st.hijack_session.hijack_id != hijack_id:  # pragma: no cover
                return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
            st.hijack_session.last_heartbeat = now
            st.hijack_session.lease_expires_at = now + lease_s
        await hub._append_event(bot_id, "hijack_heartbeat", {"hijack_id": hijack_id, "lease_s": lease_s})
        await hub._broadcast_hijack_state(bot_id)
        return {"ok": True, "hijack_id": hijack_id, "lease_expires_at": now + lease_s}

    @router.get("/bot/{bot_id}/hijack/{hijack_id}/snapshot")
    async def hijack_snapshot(
        bot_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(),
        wait_ms: int = Query(default=1500, ge=0, le=10000),
    ) -> Any:
        hs = await hub._get_rest_session(bot_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        snapshot = await hub._wait_for_snapshot(bot_id, timeout_ms=wait_ms)
        # Re-read lease_expires_at under the lock: a concurrent heartbeat may
        # have extended it during the _wait_for_snapshot poll loop.
        async with hub._lock:
            st = hub._bots.get(bot_id)
            fresh_expires = (
                st.hijack_session.lease_expires_at
                if st is not None and st.hijack_session is not None and st.hijack_session.hijack_id == hijack_id
                else hs.lease_expires_at
            )
        return {
            "ok": True,
            "bot_id": bot_id,
            "hijack_id": hijack_id,
            "snapshot": snapshot,
            "prompt_id": extract_prompt_id(snapshot),
            "lease_expires_at": fresh_expires,
        }

    @router.get("/bot/{bot_id}/hijack/{hijack_id}/events")
    async def hijack_events(
        bot_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(),
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=2000),
    ) -> Any:
        hs = await hub._get_rest_session(bot_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        async with hub._lock:
            st = hub._bots.get(bot_id)
            if st is None:  # pragma: no cover
                rows: list[dict[str, Any]] = []
                latest_seq = 0
            else:
                rows = [evt for evt in list(st.events) if int(evt.get("seq", 0)) > after_seq][:limit]
                latest_seq = st.event_seq
        return {
            "ok": True,
            "bot_id": bot_id,
            "hijack_id": hijack_id,
            "after_seq": after_seq,
            "latest_seq": latest_seq,
            "events": rows,
            "lease_expires_at": hs.lease_expires_at,
        }

    @router.post("/bot/{bot_id}/hijack/{hijack_id}/send")
    async def hijack_send(
        bot_id: str = Path(pattern=r"^[\w\-]+$"),
        hijack_id: str = Path(),
        request: HijackSendRequest = Body(...),  # noqa: B008
    ) -> Any:
        hs = await hub._get_rest_session(bot_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        if not request.keys:
            return JSONResponse({"error": "keys must not be empty."}, status_code=400)
        matched, snapshot, reason = await hub._wait_for_guard(
            bot_id,
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
        ok = await hub._send_worker(bot_id, {"type": "input", "data": request.keys, "ts": time.time()})
        if not ok:
            return JSONResponse({"error": "No worker connected for this bot."}, status_code=409)
        await hub._append_event(
            bot_id,
            "hijack_send",
            {
                "hijack_id": hijack_id,
                "keys": request.keys[:120],
                "expect_prompt_id": request.expect_prompt_id,
                "expect_regex": request.expect_regex,
            },
        )
        return {
            "ok": True,
            "bot_id": bot_id,
            "hijack_id": hijack_id,
            "sent": request.keys,
            "matched_prompt_id": extract_prompt_id(snapshot),
            "lease_expires_at": hs.lease_expires_at,
        }

    @router.post("/bot/{bot_id}/hijack/{hijack_id}/step")
    async def hijack_step(bot_id: str = Path(pattern=r"^[\w\-]+$"), hijack_id: str = Path()) -> Any:
        hs = await hub._get_rest_session(bot_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        ok = await hub._send_worker(
            bot_id, {"type": "control", "action": "step", "owner": hs.owner, "lease_s": 0, "ts": time.time()}
        )
        if not ok:
            return JSONResponse({"error": "No worker connected for this bot."}, status_code=409)
        await hub._append_event(bot_id, "hijack_step", {"hijack_id": hijack_id})
        return {"ok": True, "bot_id": bot_id, "hijack_id": hijack_id, "lease_expires_at": hs.lease_expires_at}

    @router.post("/bot/{bot_id}/hijack/{hijack_id}/release")
    async def hijack_release(bot_id: str = Path(pattern=r"^[\w\-]+$"), hijack_id: str = Path()) -> Any:
        hs = await hub._get_rest_session(bot_id, hijack_id)
        if hs is None:
            return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
        should_resume = False
        async with hub._lock:
            st = hub._bots.get(bot_id)
            if st is None or st.hijack_session is None or st.hijack_session.hijack_id != hijack_id:  # pragma: no cover
                return JSONResponse({"error": "Invalid or expired hijack session."}, status_code=404)
            st.hijack_session = None
            should_resume = st.hijack_owner is None
        if should_resume:
            await hub._send_worker(
                bot_id, {"type": "control", "action": "resume", "owner": hs.owner, "lease_s": 0, "ts": time.time()}
            )
            hub._notify_hijack_changed(bot_id, enabled=False, owner=None)
        await hub._append_event(bot_id, "hijack_released", {"hijack_id": hijack_id, "owner": hs.owner})
        await hub._broadcast_hijack_state(bot_id)
        await hub._prune_if_idle(bot_id)
        return {"ok": True, "bot_id": bot_id, "hijack_id": hijack_id}
