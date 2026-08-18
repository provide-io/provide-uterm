#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Broadcast / worker-send hot path for the message router.

Extracted from :mod:`provide.uterm.server.bridge.hub.router_impl`. Each
function takes the :class:`MessageRouter` as its first parameter and uses
the composing hub (lock, registry, output-policy gate) exactly as the
inline methods did. :class:`MessageRouter` keeps thin wrappers
(``broadcast`` / ``send_hijack_state_to`` / ``broadcast_hijack_state`` /
``send_worker``) that forward here, so the public method surface is
unchanged. Lock semantics are preserved verbatim — every ``async with
hub._lock`` block is identical to the original.

The per-send timeout constant ``_BROADCAST_SEND_TIMEOUT_S`` still lives in
``router_impl`` (tests monkeypatch it there); :func:`broadcast` reads it
through the module object so that patch keeps taking effect.

Browser sockets reach this router only after the browser route authenticates
and authorizes them, so the membership table is the authorized live terminal
stream. With no output policy gate, broadcasts preserve exact gameplay screen
semantics. A configured gate may apply role-scoped redaction. This differs from
the public event ring/EventBus/SSE/API/MCP egress, which is always redacted at
event commit time in :mod:`router_impl`.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast

from provide.telemetry import get_logger
from provide.uterm.server.bridge.frames import make_hijack_state_frame
from provide.uterm.server.bridge.hub import snapshot_metrics
from provide.uterm.server.bridge.hub.redaction import StreamRedactor
from provide.uterm.server.bridge.hub.router_redaction import _redact_frame_fields
from provide.uterm.tunnel.protocol import CHANNEL_HTTP, encode_frame

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.uterm.server.bridge.hub.router_impl import MessageRouter

logger = get_logger(__name__)


# A browser inside its startup window is not yet in the broadcast set, so
# whatever is broadcast meanwhile would be lost. That is deliberate for most
# frames and wrong for some, and the difference is whether the startup
# sequence already carries the same information.
#
# A ``term`` chunk is covered by the ``initial_snapshot`` the hello hands over,
# so replaying it would print the screen twice; ``hijack_state`` and presence
# are sent to the browser directly during startup; a newer ``snapshot``
# supersedes itself on the next one. All of those are correctly dropped.
#
# The inspect channel has no such replay. Its frames are append-only entries in
# a list the browser builds from nothing, and the store appends without dedupe
# (stores/inspectStore.ts), so one dropped ``http_req`` is a row missing for the
# life of the session with nothing to reconcile it against.
def _survives_startup_window(msg: dict[str, Any]) -> bool:
    """Whether *msg* must be held for browsers still in their startup window."""
    return msg.get("_channel") == "http"


# A browser that never finishes its startup sequence must not be able to grow
# this without limit. At the cap the queue stops accepting rather than evicting
# its oldest: dropping the newest loses the tail of a session, dropping the
# oldest loses its beginning AND renumbers everything the user already saw.
_STARTUP_BUFFER_MAX_FRAMES = 256


def _buffer_for_startup_browsers(hub: Any, st: Any, msg: dict[str, Any], worker_id: str) -> None:
    """Hold *msg* for every browser of *st* still inside its startup window.

    Caller must hold ``hub._lock``.
    """
    for ws in st.browsers:
        if ws not in hub._startup_pending_browsers:
            continue
        queued = hub._startup_pending_frames.setdefault(ws, [])
        if len(queued) >= _STARTUP_BUFFER_MAX_FRAMES:
            logger.warning(
                "startup_frame_buffer_full",
                worker_id=worker_id,
                cap=_STARTUP_BUFFER_MAX_FRAMES,
            )
            continue
        queued.append(msg)


async def activate_browser_broadcasts(hub: Any, worker_id: str, ws: WebSocket) -> None:
    """Release *ws* from its startup window, delivering what it missed first.

    The socket stays pending until its queue is empty, so a frame broadcast
    while the flush is in flight is buffered behind the ones already waiting
    instead of overtaking them. Only when nothing is left does the browser
    join the normal broadcast set — after which ``_broadcast_to_current_browsers``
    reaches it directly and this loop has nothing more to do.
    """
    from provide.uterm.server.bridge.hub import router_impl
    from provide.uterm.server.bridge.hub.core import _encode_browser_frame

    while True:
        async with hub._lock:
            queued = hub._startup_pending_frames.get(ws)
            if not queued:
                hub._startup_pending_frames.pop(ws, None)
                st = hub.registry.get(worker_id)
                # Guard preserved verbatim from before this buffered: a browser
                # that disconnected mid-startup is left pending on purpose.
                if st is not None and ws in st.browsers:
                    hub._startup_pending_browsers.discard(ws)
                return
            batch = list(queued)
            queued.clear()

        for buffered in batch:
            try:
                await asyncio.wait_for(
                    ws.send_text(_encode_browser_frame(buffered)),
                    timeout=router_impl._BROADCAST_SEND_TIMEOUT_S,
                )
            except Exception:
                # A socket that cannot take its own backlog is gone. Drop the
                # backlog, but leave it PENDING: pending means the broadcast
                # path skips it, which is what you want for a socket that just
                # failed a write. The route's disconnect handler removes it
                # from both.
                logger.warning("startup_frame_flush_failed", worker_id=worker_id)
                async with hub._lock:
                    hub._startup_pending_frames.pop(ws, None)
                return


_HTTP_INSPECT_CONTROL_TYPES = frozenset(
    {
        "http_action",
        "http_intercept_toggle",
        "http_inspect_toggle",
    }
)


async def payloads_by_role(
    router: MessageRouter,
    worker_id: str,
    msg: dict[str, Any],
    browsers_with_roles: list[tuple[WebSocket, str]],
    encoded_default: str,
) -> dict[str | None, str]:
    """Pre-resolve the encoded payload for every DISTINCT role, once each.

    Redaction rules are role-scoped, so the policy context + redacted payload
    only need to be built once per distinct viewer role per frame — not once
    per browser. This caps the per-frame ``prepare_policy_context`` calls
    (each re-acquires ``hub._lock``) and ``get_redaction_rules`` calls at the
    number of distinct roles, instead of letting N viewers trigger N policy
    builds + N lock acquisitions.

    Resolved SEQUENTIALLY and up front (rather than lazily inside the per-
    browser loop) precisely so the concurrent send fan-out in
    :func:`broadcast` reads an immutable mapping rather than racing on a
    shared lazy cache and triggering duplicate policy builds.
    """
    from provide.uterm.server.bridge.hub.core import _encode_browser_frame

    hub = router._hub
    by_role: dict[str | None, str] = {}
    for ws, role in browsers_with_roles:
        if role in by_role:
            continue
        context = await hub.prepare_policy_context(ws, worker_id, action="output")
        rules = await hub._output_policy_gate.get_redaction_rules(context)  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]  # None case already guarded for mypy via the type: ignore above
        if (
            rules
        ):  # pragma: no branch — empty-rules fall-through is the default state; covered by output-gate unit tests
            by_role[role] = _encode_browser_frame(_redact_frame_fields(msg, StreamRedactor(rules)))
        else:
            by_role[role] = encoded_default
    return by_role


def _is_current_snapshot(
    state: Any,
    *,
    expected_worker: WebSocket,
    expected_event_seq: int,
) -> bool:
    """Return whether *state* still owns the snapshot being broadcast."""
    return (
        state.worker_ws is expected_worker
        and state.last_snapshot is not None
        and state.last_snapshot.get("event_seq") == expected_event_seq
    )


async def broadcast(
    router: MessageRouter,
    worker_id: str,
    msg: dict[str, Any],
    *,
    expected_worker: WebSocket | None = None,
    expected_event_seq: int | None = None,
) -> None:
    """Send *msg* to all browser WebSockets registered for *worker_id*.

    Registered browsers are authenticated, authorized live viewers. The
    default payload is therefore the exact terminal frame; deployments that
    configure an output policy gate receive role-scoped redaction instead.

    Sends are fanned out CONCURRENTLY (``asyncio.gather``) so a slow browser
    only consumes its own ``_BROADCAST_SEND_TIMEOUT_S`` budget instead of
    head-of-line-blocking every later browser by up to that timeout. Role-
    scoped redaction payloads are pre-computed up front (see
    :func:`payloads_by_role`) so the concurrent tasks read an immutable
    mapping rather than racing on a lazy cache.
    """
    hub = router._hub

    snapshot_fence = None
    async with hub._lock:
        st = hub.registry.get(worker_id)
        if st is None:
            return
        # pragma: no branch — the false arm leaves the `async with` block, and
        # coverage.py records that __aexit__ arc as partial only on 3.11; the
        # identical test selection reports it covered on 3.12/3.13/3.14. Same
        # quirk already pragma'd in connection_hijack.py.
        if expected_worker is not None or expected_event_seq is not None:  # pragma: no branch
            if expected_worker is None or expected_event_seq is None:
                return
            if not _is_current_snapshot(
                st,
                expected_worker=expected_worker,
                expected_event_seq=expected_event_seq,
            ):
                return
            snapshot_fence = st.snapshot_egress_fence

    if snapshot_fence is not None:
        async with snapshot_fence:
            await _broadcast_to_current_browsers(
                router,
                worker_id,
                msg,
                expected_worker=expected_worker,
                expected_event_seq=expected_event_seq,
            )
        return

    await _broadcast_to_current_browsers(router, worker_id, msg)


async def _broadcast_to_current_browsers(
    router: MessageRouter,
    worker_id: str,
    msg: dict[str, Any],
    *,
    expected_worker: WebSocket | None = None,
    expected_event_seq: int | None = None,
) -> None:
    """Broadcast after validating an optional snapshot ownership contract."""
    from provide.uterm.server.bridge.hub import router_impl
    from provide.uterm.server.bridge.hub.core import _encode_browser_frame

    timeout_s = router_impl._BROADCAST_SEND_TIMEOUT_S
    hub = router._hub
    async with hub._lock:
        st = hub.registry.get(worker_id)
        if st is None:
            return
        if (
            expected_worker is not None
            and expected_event_seq is not None
            and not _is_current_snapshot(
                st,
                expected_worker=expected_worker,
                expected_event_seq=expected_event_seq,
            )
        ):
            return
        browsers_with_roles = [
            (ws, role) for ws, role in st.browsers.items() if ws not in hub._startup_pending_browsers
        ]
        if _survives_startup_window(msg):
            _buffer_for_startup_browsers(hub, st, msg, worker_id)

    # A committed snapshot that reaches nobody is the failure this names. The
    # commit succeeds, the fan-out below sends to an empty set and returns
    # normally, and a polling client keeps reading the previous screen — which
    # is indistinguishable from an idle terminal. Terminal data streams
    # constantly with no viewer attached, so only snapshots are worth saying.
    if not browsers_with_roles and msg.get("type") == "snapshot":
        # TRACE, not warning: the hijack path registers no browsers at all, so
        # this fires on essentially every snapshot — 9,881 times in one measured
        # run, every one of them benign. At warning level it buried the two
        # lines beside it that fire only on a real fault. ``trace`` is a no-op
        # unless TRACE is explicitly enabled, so the line costs nothing until
        # somebody goes looking for it.
        snapshot_metrics.snapshot_broadcast_no_browsers.add(1, {"worker_id": worker_id})
        logger.trace(
            "snapshot_broadcast_no_browsers",
            worker_id=worker_id,
            screen_hash=msg.get("screen_hash"),
            registered=len(st.browsers),
        )

    # Pre-encode for all browsers (except when redaction is needed).
    encoded_default = _encode_browser_frame(msg)

    gate_active = bool(hub._output_policy_gate) and msg.get("type") in {"term", "snapshot", "analysis"}
    # When the gate is active, payloads_by_role resolves an entry for every
    # role present in browsers_with_roles, so the per-send lookup below is a
    # total mapping (no default needed). When inactive, every browser gets
    # the single shared default payload.
    payload_by_role = (
        await payloads_by_role(router, worker_id, msg, browsers_with_roles, encoded_default) if gate_active else {}
    )

    # Policy evaluation above may await. Revalidate after it, immediately
    # before the socket sends, so a replacement or newer committed sequence
    # cannot pass an earlier ownership check and then emit stale output.
    if expected_worker is not None and expected_event_seq is not None:
        async with hub._lock:
            current = hub.registry.get(worker_id)
            if current is None or not _is_current_snapshot(
                current,
                expected_worker=expected_worker,
                expected_event_seq=expected_event_seq,
            ):
                return

    async def _send(ws: WebSocket, role: str) -> None:
        payload = payload_by_role[role] if gate_active else encoded_default
        await asyncio.wait_for(ws.send_text(payload), timeout=timeout_s)

    # 0/1 browser: a single send has no head-of-line problem, so await it
    # directly. This skips gather()'s extra event-loop turn and keeps the
    # post-broadcast ordering identical to the legacy sequential path. With
    # 2+ browsers we fan out concurrently so one slow viewer never delays
    # the others by up to _BROADCAST_SEND_TIMEOUT_S.
    if len(browsers_with_roles) <= 1:
        results: list[BaseException | None] = []
        for ws, role in browsers_with_roles:
            try:
                await _send(ws, role)
                results.append(None)
            except Exception as exc:
                # Mirror gather(return_exceptions=True): capture, don't raise.
                results.append(exc)
    else:
        results = list(
            await asyncio.gather(*(_send(ws, role) for ws, role in browsers_with_roles), return_exceptions=True)
        )

    dead: set[WebSocket] = set()
    for (ws, _role), result in zip(browsers_with_roles, results, strict=True):
        if isinstance(result, Exception):
            logger.debug("broadcast_send_failed worker_id=%s: %s", worker_id, result)
            if msg.get("type") == "snapshot":
                # DEBUG is below the level a manager runs at, so a snapshot send
                # that timed out left no production trace at all — the frame was
                # committed, the fan-out "succeeded", and the client went on
                # reading the previous screen. Say it loudly for snapshots only;
                # term frames fail often enough on a closing socket that raising
                # them would bury this.
                snapshot_metrics.snapshot_broadcast_send_failed.add(1, {"worker_id": worker_id})
                logger.warning(
                    "snapshot_broadcast_send_failed",
                    worker_id=worker_id,
                    screen_hash=msg.get("screen_hash"),
                    error=str(result)[:200],
                )
            dead.add(ws)
    if dead:
        changed = await hub.remove_dead_browsers(worker_id, dead)
        if changed:
            await router.broadcast_hijack_state(worker_id)


async def send_hijack_state_to(
    router: MessageRouter,
    browsers: list[WebSocket],
    *,
    worker_id: str,
    is_hijacked: bool,
    is_dashboard: bool,
    is_rest: bool,
    hijack_owner: WebSocket | None,
    input_mode: str,
    lease_expires_at: float | None,
    suppress_errors: bool = False,
) -> set[WebSocket]:
    """Send a hijack_state message to each browser; return the set of dead sockets."""
    from provide.uterm.server.bridge.hub import router_impl
    from provide.uterm.server.bridge.hub.core import _encode_browser_frame, _mono_to_wall

    timeout_s = router_impl._BROADCAST_SEND_TIMEOUT_S
    dead: set[WebSocket] = set()
    for ws in browsers:
        if is_dashboard and hijack_owner is ws:
            owner: str | None = "me"
        elif is_dashboard or is_rest:
            owner = "other"
        else:
            owner = None
        payload = _encode_browser_frame(
            cast(
                "dict[str, Any]",
                make_hijack_state_frame(
                    hijacked=is_hijacked,
                    owner=owner,
                    lease_expires_at=_mono_to_wall(lease_expires_at),
                    input_mode=input_mode,
                ),
            )
        )
        try:
            # Per-send timeout so one stalled browser can't head-of-line-block
            # the hijack-state notification to the rest (parity with broadcast()).
            await asyncio.wait_for(ws.send_text(payload), timeout=timeout_s)
        except Exception as exc:
            if not suppress_errors:
                logger.debug("broadcast_hijack_state_send_failed worker_id=%s: %s", worker_id, exc)
            dead.add(ws)
    return dead


async def broadcast_hijack_state(router: MessageRouter, worker_id: str) -> None:
    """Send a hijack_state message to every browser for *worker_id*, cleaning up dead sockets."""
    hub = router._hub
    async with hub._lock:
        st = hub.registry.get(worker_id)
        if st is None:
            return
        browsers = [ws for ws in st.browsers if ws not in hub._startup_pending_browsers]
        hijack_owner = st.hijack_owner
        is_hijacked = hub.is_hijacked(st)
        is_dashboard = hub.is_dashboard_hijack_active(st)
        is_rest = hub.has_valid_rest_lease(st)
        input_mode = st.input_mode
        lease_expires_at = (
            st.hijack_session.lease_expires_at
            if is_rest and st.hijack_session is not None
            else st.hijack_owner_expires_at
        )

    dead = await send_hijack_state_to(
        router,
        browsers,
        worker_id=worker_id,
        is_hijacked=is_hijacked,
        is_dashboard=is_dashboard,
        is_rest=is_rest,
        hijack_owner=hijack_owner,
        input_mode=input_mode,
        lease_expires_at=lease_expires_at,
    )
    if dead:
        await hub.remove_dead_browsers(worker_id, dead)
        async with hub._lock:
            st2 = hub.registry.get(worker_id)
            if st2 is None:
                return
            survivors = [ws for ws in st2.browsers if ws not in hub._startup_pending_browsers]
            is_h2 = hub.is_hijacked(st2)
            is_dashboard2 = hub.is_dashboard_hijack_active(st2)
            is_rest2 = hub.has_valid_rest_lease(st2)
            hijack_owner2 = st2.hijack_owner
            input_mode2 = st2.input_mode
            lease2 = (
                st2.hijack_session.lease_expires_at
                if is_rest2 and st2.hijack_session is not None
                else st2.hijack_owner_expires_at
            )
        await send_hijack_state_to(
            router,
            survivors,
            worker_id=worker_id,
            is_hijacked=is_h2,
            is_dashboard=is_dashboard2,
            is_rest=is_rest2,
            hijack_owner=hijack_owner2,
            input_mode=input_mode2,
            lease_expires_at=lease2,
            suppress_errors=True,
        )


async def send_worker(
    router: MessageRouter,
    worker_id: str,
    msg: dict[str, Any],
    *,
    source: Any = None,
    expected_worker: WebSocket | None = None,
) -> bool:
    """Send *msg* to the worker WebSocket; returns False if no worker is connected.

    Tunnel workers (``is_tunnel_worker=True``) use the binary tunnel
    protocol: ``input`` messages are sent as raw UTF-8 PTY bytes, HTTP
    inspect controls are sent on ``CHANNEL_HTTP``, and other message
    types are dropped because the worker's bridge loop has no JSON
    envelope handling.
    """
    from provide.uterm.server.bridge.hub.core import _encode_worker_frame

    hub = router._hub
    if source and msg.get("type") == "input":
        router.record_keystroke(source)

    async with hub._lock:
        st = hub.registry.get(worker_id)
        if st is None or st.worker_ws is None:
            return False
        if expected_worker is not None and st.worker_ws is not expected_worker:
            return False
        ws = st.worker_ws
        is_tunnel = st.is_tunnel_worker
    try:
        if is_tunnel:
            # Tunnel wire format: hub → worker input is raw PTY bytes;
            # inspect/intercept commands use the HTTP side-channel.
            msg_type = msg.get("type")
            if msg_type in _HTTP_INSPECT_CONTROL_TYPES:
                payload = json.dumps(msg, separators=(",", ":")).encode()
                await ws.send_bytes(encode_frame(CHANNEL_HTTP, payload))
                return True
            if msg_type != "input":
                return True
            data = msg.get("data")
            if not isinstance(data, str):
                return True
            await ws.send_bytes(data.encode("utf-8"))
            return True
        await ws.send_text(_encode_worker_frame(msg))
        return True
    except BaseException as exc:
        logger.debug("send_worker_failed worker_id=%s: %s", worker_id, exc)
        async with hub._lock:
            st2 = hub.registry.get(worker_id)
            if st2 is not None and st2.worker_ws is ws:  # pragma: no branch
                st2.worker_ws = None
        if isinstance(exc, Exception):
            return False
        raise


__all__ = [
    "broadcast",
    "broadcast_hijack_state",
    "payloads_by_role",
    "send_hijack_state_to",
    "send_worker",
]
