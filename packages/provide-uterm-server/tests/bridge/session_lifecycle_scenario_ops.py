#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Ownership-handoff and approval-expiry executors for the Python native adapter.

These two scenario executors live beside ``test_session_lifecycle_security_scenarios``
rather than inside it purely to keep that adapter under the repo's per-file LOC
cap; they drive the same really-served app through the same public routes and
reuse the adapter's fixtures. The adapter imports this module from inside its
dispatch function so the shared-fixture import below is not a cycle.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx2
import websockets

from .test_session_lifecycle_security_scenarios import (
    ADMIN_HEADERS,
    WORKER_HEADERS,
    _acquire_browser_owner,
    _configured_app,
    _decode_events,
    _drain_browser_startup,
    _drain_worker_startup,
    _observation,
    _policy_server,
    _principal_headers,
    _receive_matching,
    _receive_through,
    _send_control,
    _serve,
    _ws_url,
)

# Normalized tokens for the approval route's human-readable HTTP details. An
# unmapped detail is reported verbatim so a changed refusal surfaces as a
# contract mismatch instead of being silently normalized away.
APPROVAL_ERRORS = {"Approval request is not pending": "approval_not_pending"}


async def _delivered_data(websocket: Any, *, timeout: float = 0.15) -> list[str]:
    """Collect every raw data chunk the peer actually received within *timeout*."""
    delivered: list[str] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=deadline - asyncio.get_running_loop().time())
        except TimeoutError:
            break
        delivered.extend(str(event["data"]) for event in _decode_events(raw) if event.get("type") == "data")
    return delivered


async def _release_browser_owner(browser: Any, worker: Any) -> bool:
    """Release the WS hijack lease and confirm both ends observed the release."""
    await _send_control(browser, {"type": "hijack_release"})
    await _receive_matching(
        worker,
        lambda event: event.get("type") == "control" and event.get("action") == "resume",
    )
    released = await _receive_matching(
        browser,
        lambda event: event.get("type") == "hijack_state" and event.get("hijacked") is False,
    )
    return released.get("owner") is None


async def execute_owner_handoff(scenario: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Release the lease as owner A, acquire it as successor B, then race their input."""
    input_data = scenario["input"]
    worker_id = input_data["worker_id"]
    payload = input_data["payload"]

    def is_payload(event: dict[str, Any]) -> bool:
        return event.get("type") == "data" and event.get("data") == payload

    app = _configured_app(worker_id)
    async with _serve(app, "python owner handoff") as base_url:
        worker_url = _ws_url(base_url, f"/ws/worker/{worker_id}/term")
        browser_url = _ws_url(base_url, f"/ws/browser/{worker_id}/term")
        outgoing_headers = _principal_headers(input_data["outgoing_owner"])
        incoming_headers = _principal_headers(input_data["incoming_owner"])
        async with (
            websockets.connect(worker_url, additional_headers=WORKER_HEADERS) as worker,
            websockets.connect(browser_url, additional_headers=outgoing_headers) as outgoing,
        ):
            await _drain_worker_startup(worker)
            await _drain_browser_startup(outgoing)
            await _acquire_browser_owner(outgoing, worker)
            async with websockets.connect(browser_url, additional_headers=incoming_headers) as incoming:
                await _drain_browser_startup(incoming)
                released = await _release_browser_owner(outgoing, worker)
                await _acquire_browser_owner(incoming, worker)
                successor_view = await _receive_matching(
                    outgoing,
                    lambda event: event.get("type") == "hijack_state" and event.get("owner") == "other",
                )
                # The outgoing owner keeps its socket and speaks after releasing.
                await _send_control(outgoing, {"type": "input", "data": payload})
                await _send_control(outgoing, {"type": "ping"})
                await _receive_through(outgoing, lambda event: event.get("type") == "pong")
                stale_delivered = await _delivered_data(worker)

                await _send_control(incoming, {"type": "input", "data": payload})
                accepted = await _receive_matching(worker, is_payload)
                delivered = [*stale_delivered, str(accepted["data"]), *await _delivered_data(worker)]

    return _observation(
        scenario,
        defaults,
        route="browser_websocket",
        status_code=101,
        handoff_completed=released and successor_view.get("hijacked") is True,
        stale_owner_refused=not stale_delivered,
        successor_owner_accepted=accepted.get("data") == payload,
        delivered_payloads=delivered,
    )


async def execute_approval_expiry(scenario: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Hold a command past its approval deadline, then claim it late over HTTP."""
    input_data = scenario["input"]
    worker_id = input_data["worker_id"]
    payload = input_data["payload"]
    approver_headers = _principal_headers(input_data["principal"])
    # timeout_s=0 puts the hold deadline at creation time, so the late claim
    # races nothing: the request is already past its deadline when the approve
    # arrives, with no sleep and no wall-clock tolerance.
    async with _policy_server("hold", timeout_s=0) as (policy_url, _calls):
        app = _configured_app(worker_id, policy_url=policy_url)
        async with _serve(app, "python approval expiry") as base_url:
            worker_url = _ws_url(base_url, f"/ws/worker/{worker_id}/term")
            browser_url = _ws_url(base_url, f"/ws/browser/{worker_id}/term")
            async with (
                websockets.connect(worker_url, additional_headers=WORKER_HEADERS) as worker,
                websockets.connect(browser_url, additional_headers=ADMIN_HEADERS) as browser,
            ):
                await _drain_worker_startup(worker)
                await _drain_browser_startup(browser)
                await _acquire_browser_owner(browser, worker)
                await _send_control(browser, {"type": "input", "data": payload})
                held = await _receive_matching(browser, lambda event: event.get("type") == "approval_pending")
                request_id = str(held["request_id"])
                async with httpx2.AsyncClient(timeout=5.0) as client:
                    approve = await client.post(
                        f"{base_url}/api/approvals/{request_id}/approve", headers=approver_headers
                    )
                    listed = await client.get(f"{base_url}/api/approvals", headers=approver_headers)
                expired = await _receive_matching(
                    browser,
                    lambda event: event.get("type") == "approval_resolved" and event.get("request_id") == request_id,
                )
                delivered = await _delivered_data(worker)

    detail = str(approve.json().get("detail", ""))
    observed_error = APPROVAL_ERRORS.get(detail, detail)
    still_pending = any(str(item["id"]) == request_id for item in listed.json())
    return _observation(
        scenario,
        defaults,
        route="http",
        status_code=approve.status_code,
        error=observed_error,
        approval_expired=expired.get("outcome") == "timeout" and not still_pending,
        late_approval_refused=(
            approve.status_code == 400 and observed_error == "approval_not_pending" and not delivered
        ),
        delivered_payloads=delivered,
    )
