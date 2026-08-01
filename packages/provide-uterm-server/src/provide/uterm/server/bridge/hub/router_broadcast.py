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
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast

from provide.telemetry import get_logger
from provide.uterm.server.bridge.frames import make_hijack_state_frame
from provide.uterm.server.bridge.hub.redaction import StreamRedactor
from provide.uterm.server.bridge.hub.router_redaction import _redact_frame_fields
from provide.uterm.tunnel.protocol import CHANNEL_HTTP, encode_frame

if TYPE_CHECKING:
    from fastapi import WebSocket

    from provide.uterm.server.bridge.hub.router_impl import MessageRouter

logger = get_logger(__name__)

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


async def broadcast(router: MessageRouter, worker_id: str, msg: dict[str, Any]) -> None:
    """Send *msg* to all browser WebSockets registered for *worker_id*.

    Sends are fanned out CONCURRENTLY (``asyncio.gather``) so a slow browser
    only consumes its own ``_BROADCAST_SEND_TIMEOUT_S`` budget instead of
    head-of-line-blocking every later browser by up to that timeout. Role-
    scoped redaction payloads are pre-computed up front (see
    :func:`payloads_by_role`) so the concurrent tasks read an immutable
    mapping rather than racing on a lazy cache.
    """
    from provide.uterm.server.bridge.hub import router_impl
    from provide.uterm.server.bridge.hub.core import _encode_browser_frame

    timeout_s = router_impl._BROADCAST_SEND_TIMEOUT_S
    hub = router._hub
    async with hub._lock:
        st = hub.registry.get(worker_id)
        if st is None:
            return
        browsers_with_roles = [
            (ws, role) for ws, role in st.browsers.items() if ws not in hub._startup_pending_browsers
        ]

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
