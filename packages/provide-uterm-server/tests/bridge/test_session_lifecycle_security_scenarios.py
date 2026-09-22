#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Python native adapter for public-route session-lifecycle security scenarios."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from provide.uterm.control_channel import encode_control_frame
from provide.uterm.server.webhook_signing import verify_webhook_signature
from provide.uterm.tunnel.protocol import CHANNEL_DATA, encode_frame

from .session_lifecycle_scenario_ops import execute_approval_expiry, execute_owner_handoff
from .session_lifecycle_scenario_support import (
    ADMIN_HEADERS,
    POLICY_SECRET,
    WORKER_HEADERS,
    _acquire_browser_owner,
    _configured_app,
    _drain_browser_startup,
    _drain_worker_startup,
    _matching_count,
    _observation,
    _policy_server,
    _principal_headers,
    _receive_matching,
    _receive_through,
    _send_control,
    _serve,
    _ws_url,
)

CONTRACT_PATH = Path(
    os.environ.get(
        "SESSION_LIFECYCLE_SCENARIO_CONTRACT",
        Path(__file__).resolve().parents[4] / "spec/session_lifecycle_security_scenarios.json",
    )
)
OUTPUT_PATH = os.environ.get("SESSION_LIFECYCLE_SCENARIO_OUTPUT")


async def _fragment_stream(
    payload: str | bytes,
    fragment_count: int,
    first_fragment_sent: asyncio.Event,
    release_final_fragments: asyncio.Event,
) -> AsyncIterator[str | bytes]:
    width = max(1, len(payload) // fragment_count)
    fragments = [payload[index : index + width] for index in range(0, len(payload), width)]
    while len(fragments) > fragment_count:
        fragments[-2:] = [fragments[-2] + fragments[-1]]
    yield fragments[0]
    first_fragment_sent.set()
    await release_final_fragments.wait()
    for fragment in fragments[1:]:
        yield fragment


async def _fragmented_action_counts(
    sender: Any,
    payload: str | bytes,
    fragment_count: int,
    receiver: Any,
    predicate: Callable[[dict[str, Any]], bool],
) -> tuple[int, int]:
    first_fragment_sent = asyncio.Event()
    release_final_fragments = asyncio.Event()
    send_task = asyncio.create_task(
        sender.send(_fragment_stream(payload, fragment_count, first_fragment_sent, release_final_fragments))
    )
    await asyncio.wait_for(first_fragment_sent.wait(), timeout=1.0)
    pre_final = await _matching_count(receiver, predicate)
    release_final_fragments.set()
    await asyncio.wait_for(send_task, timeout=2.0)
    await _receive_matching(receiver, predicate)
    post_final = 1 + await _matching_count(receiver, predicate)
    return pre_final, post_final


async def _execute_fragmentation(scenario: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    input_data = scenario["input"]
    worker_id = input_data["worker_id"]
    transport = input_data["transport"]
    payload = input_data["payload"]
    fragment_count = input_data["fragment_count"]
    oversized = input_data["oversized_bytes"]

    def is_payload(event: dict[str, Any]) -> bool:
        return event.get("type") == "data" and event.get("data") == payload

    app = _configured_app(worker_id)
    async with _serve(app, f"python {transport} fragmentation") as base_url:
        browser_url = _ws_url(base_url, f"/ws/browser/{worker_id}/term")
        worker_url = _ws_url(base_url, f"/ws/worker/{worker_id}/term")
        if transport == "browser":
            async with (
                websockets.connect(worker_url, additional_headers=WORKER_HEADERS) as worker,
                websockets.connect(browser_url, additional_headers=ADMIN_HEADERS) as browser,
            ):
                await _drain_worker_startup(worker)
                await _drain_browser_startup(browser)
                await _acquire_browser_owner(browser, worker)
                encoded = encode_control_frame({"type": "input", "data": payload})
                pre_final, post_final = await _fragmented_action_counts(
                    browser, encoded, fragment_count, worker, is_payload
                )
                await _send_control(browser, {"type": "input", "data": "X" * oversized})
                await _send_control(browser, {"type": "ping"})
                await _receive_matching(browser, lambda event: event.get("type") == "pong")
                oversized_refused = await _matching_count(worker, lambda event: event.get("type") == "data") == 0
            route = "browser_websocket"
        elif transport == "worker":
            async with (
                websockets.connect(worker_url, additional_headers=WORKER_HEADERS) as worker,
                websockets.connect(browser_url, additional_headers=ADMIN_HEADERS) as browser,
            ):
                await _drain_worker_startup(worker)
                await _drain_browser_startup(browser)
                pre_final, post_final = await _fragmented_action_counts(
                    worker, payload, fragment_count, browser, is_payload
                )
                await worker.send("X" * oversized)
                barrier_payload = "worker-oversize-ordering-barrier"
                await worker.send(barrier_payload)
                ordered_events = await _receive_through(
                    browser,
                    lambda event: event.get("type") == "data" and event.get("data") == barrier_payload,
                )
                oversized_refused = not any(
                    event.get("type") == "data" and event.get("data") == "X" * oversized for event in ordered_events
                )
            route = "worker_websocket"
        else:
            tunnel_url = _ws_url(base_url, f"/tunnel/{worker_id}")
            async with (
                websockets.connect(tunnel_url, additional_headers=WORKER_HEADERS) as tunnel,
                websockets.connect(browser_url, additional_headers=ADMIN_HEADERS) as browser,
            ):
                await _drain_browser_startup(browser)
                frame = encode_frame(CHANNEL_DATA, payload.encode())
                pre_final, post_final = await _fragmented_action_counts(
                    tunnel, frame, fragment_count, browser, is_payload
                )
                await tunnel.send(encode_frame(CHANNEL_DATA, b"X" * oversized))
                barrier_payload = "tunnel-oversize-ordering-barrier"
                await tunnel.send(encode_frame(CHANNEL_DATA, barrier_payload.encode()))
                ordered_events = await _receive_through(
                    browser,
                    lambda event: event.get("type") == "data" and event.get("data") == barrier_payload,
                )
                oversized_refused = not any(
                    event.get("type") == "data" and event.get("data") == "X" * oversized for event in ordered_events
                )
            route = "tunnel_websocket"
    return _observation(
        scenario,
        defaults,
        route=route,
        status_code=101,
        fragment_count=fragment_count,
        pre_final_actions=pre_final,
        post_final_actions=post_final,
        oversized_refused=oversized_refused,
        delivered_payloads=[payload],
    )


async def _connect_admitted_browser(url: str, headers: dict[str, str], *, timeout: float = 2.0) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        websocket = await websockets.connect(url, additional_headers=headers)
        try:
            await _drain_browser_startup(websocket)
            return websocket
        except ConnectionClosed:
            await websocket.close()
            await asyncio.sleep(0.01)
    raise TimeoutError("browser quota slot was not released")


async def _observe_failed_browser_setup(url: str, headers: dict[str, str]) -> bool:
    websocket = await websockets.connect(url, additional_headers=headers)
    try:
        while True:
            await asyncio.wait_for(websocket.recv(), timeout=2.0)
    except ConnectionClosed:
        return True
    finally:
        await websocket.close()


async def _execute_quota(scenario: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    input_data = scenario["input"]
    worker_id = input_data["worker_id"]
    headers = _principal_headers(input_data["principal"])
    app = _configured_app(
        worker_id,
        max_connections=input_data["limit"],
        fail_browser_setup_once=True,
    )
    async with _serve(app, "python browser quota") as base_url:
        url = _ws_url(base_url, f"/ws/browser/{worker_id}/term")

        setup_failed = await _observe_failed_browser_setup(url, headers)
        first = await _connect_admitted_browser(url, headers)
        setup_rollback_verified = (
            setup_failed and app.state.lifecycle_setup_failure_triggered and first.state.name == "OPEN"
        )

        rejected = await websockets.connect(url, additional_headers=headers)
        close_code = 0
        try:
            await rejected.recv()
        except ConnectionClosed as exc:
            close_code = exc.rcvd.code if exc.rcvd is not None else exc.code
        finally:
            await rejected.close()

        await first.close()
        recovered = await _connect_admitted_browser(url, headers)
        quota_recovered = recovered.state.name == "OPEN"
        await recovered.close()

    return _observation(
        scenario,
        defaults,
        route="browser_websocket",
        status_code=close_code,
        error="too_many_connections",
        accepted_connections=2,
        rejected_connections=1,
        quota_recovered=quota_recovered,
        setup_rollback_verified=setup_rollback_verified,
    )


async def _execute_governance(scenario: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    input_data = scenario["input"]
    worker_id = input_data["worker_id"]
    decision = input_data["decision"]
    payload = input_data["payload"]
    delivered: list[str] = []
    observed_error: str | None = None
    observed_policy_decision: str | None = None
    async with _policy_server(decision) as (policy_url, calls):
        app = _configured_app(worker_id, policy_url=policy_url)
        async with _serve(app, f"python configured governance {decision}") as base_url:
            worker_url = _ws_url(base_url, f"/ws/worker/{worker_id}/term")
            browser_url = _ws_url(base_url, f"/ws/browser/{worker_id}/term")
            async with (
                websockets.connect(worker_url, additional_headers=WORKER_HEADERS) as worker,
                websockets.connect(browser_url, additional_headers=ADMIN_HEADERS) as browser,
            ):
                await _drain_worker_startup(worker)
                await _drain_browser_startup(browser)
                await _acquire_browser_owner(browser, worker)
                await _send_control(browser, {"type": "input", "data": payload + "\n"})
                if decision == "allow":
                    observed = await _receive_matching(
                        worker,
                        lambda event: event.get("type") == "data" and event.get("data") == payload + "\n",
                    )
                    if observed:
                        delivered.append(payload)
                        observed_policy_decision = "allow"
                else:
                    error_frame = await _receive_matching(browser, lambda event: event.get("type") == "error")
                    assert error_frame.get("message") == f"Command part blocked by policy: {payload}\n"
                    downstream = await _matching_count(worker, lambda event: event.get("type") == "data")
                    assert downstream == 0
                    observed_error = "policy_denied" if decision == "deny" else "governance_unavailable"
                    observed_policy_decision = decision

    assert calls, "configured governance route did not call the policy listener"
    body, headers = calls[-1]
    signed_request = verify_webhook_signature(
        POLICY_SECRET,
        body,
        headers.get("x-uterm-signature"),
        headers.get("x-uterm-timestamp"),
    )
    return _observation(
        scenario,
        defaults,
        route="browser_websocket",
        status_code=101,
        error=observed_error,
        policy_decision=observed_policy_decision,
        signed_request=signed_request,
        delivered_payloads=delivered,
    )


async def _execute_resume(scenario: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    input_data = scenario["input"]
    worker_id = input_data["worker_id"]
    principal_headers = _principal_headers(input_data["principal"])
    app = _configured_app(worker_id)
    ownership_restored = False
    resume_succeeded = False
    replay_rejected = False
    competing_owner_preserved = False
    async with _serve(app, f"python resume {input_data['case']}") as base_url:
        worker_url = _ws_url(base_url, f"/ws/worker/{worker_id}/term")
        browser_url = _ws_url(base_url, f"/ws/browser/{worker_id}/term")
        async with websockets.connect(worker_url, additional_headers=WORKER_HEADERS) as worker:
            await _drain_worker_startup(worker)
            original = await websockets.connect(browser_url, additional_headers=principal_headers)
            hello = await _drain_browser_startup(original)
            token = hello["resume_token"]
            assert isinstance(token, str) and token
            await _acquire_browser_owner(original, worker)
            await original.close()
            await _receive_matching(
                worker,
                lambda event: event.get("type") == "control" and event.get("action") == "resume",
            )

            if input_data["case"] == "current_owner":
                resumed = await websockets.connect(browser_url, additional_headers=principal_headers)
                await _drain_browser_startup(resumed)
                await _send_control(resumed, {"type": "resume", "token": token})
                resumed_hello = await _receive_matching(
                    resumed,
                    lambda event: event.get("type") == "hello" and event.get("resumed") is True,
                )
                state = await _receive_matching(
                    resumed,
                    lambda event: event.get("type") == "hijack_state" and event.get("owner") == "me",
                )
                await _receive_matching(
                    worker,
                    lambda event: event.get("type") == "control" and event.get("action") == "pause",
                )
                ownership_restored = resumed_hello["resumed"] is True and state["owner"] == "me"
                resume_succeeded = ownership_restored

                replay = await websockets.connect(browser_url, additional_headers=principal_headers)
                await _drain_browser_startup(replay)
                await _send_control(replay, {"type": "resume", "token": token})
                await _send_control(replay, {"type": "ping"})
                replay_events = await _receive_through(
                    replay,
                    lambda event: event.get("type") == "pong",
                )
                replay_rejected = not any(
                    event.get("type") == "hello" and event.get("resumed") is True for event in replay_events
                )
                await replay.close()
                await resumed.close()
            else:
                competitor_headers = _principal_headers(input_data["competing_principal"])
                competitor = await websockets.connect(browser_url, additional_headers=competitor_headers)
                await _drain_browser_startup(competitor)
                await _acquire_browser_owner(competitor, worker)

                resumed = await websockets.connect(browser_url, additional_headers=principal_headers)
                await _drain_browser_startup(resumed)
                await _send_control(resumed, {"type": "resume", "token": token})
                await _send_control(resumed, {"type": "ping"})
                stale_events = await _receive_through(resumed, lambda event: event.get("type") == "pong")
                resume_succeeded = any(
                    event.get("type") == "hello" and event.get("resumed") is True for event in stale_events
                )
                await _send_control(competitor, {"type": "heartbeat"})
                heartbeat = await _receive_matching(competitor, lambda event: event.get("type") == "heartbeat_ack")
                competing_owner_preserved = not resume_succeeded and heartbeat["type"] == "heartbeat_ack"
                await resumed.close()
                await competitor.close()

    return _observation(
        scenario,
        defaults,
        route="browser_websocket",
        status_code=101,
        resume_succeeded=resume_succeeded,
        ownership_restored=ownership_restored,
        replay_rejected=replay_rejected,
        competing_owner_preserved=competing_owner_preserved,
    )


async def _execute_non_owner_step(scenario: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    input_data = scenario["input"]
    worker_id = input_data["worker_id"]
    app = _configured_app(worker_id)
    async with _serve(app, "python non-owner hijack step") as base_url:
        worker_url = _ws_url(base_url, f"/ws/worker/{worker_id}/term")
        browser_url = _ws_url(base_url, f"/ws/browser/{worker_id}/term")
        async with (
            websockets.connect(worker_url, additional_headers=WORKER_HEADERS) as worker,
            websockets.connect(browser_url, additional_headers=_principal_headers(input_data["owner"])) as owner,
            websockets.connect(
                browser_url,
                additional_headers=_principal_headers(input_data["non_owner"]),
            ) as non_owner,
        ):
            await _drain_worker_startup(worker)
            await _drain_browser_startup(owner)
            await _drain_browser_startup(non_owner)
            await _acquire_browser_owner(owner, worker)
            await _send_control(non_owner, {"type": "hijack_step"})
            await _send_control(non_owner, {"type": "ping"})
            await _receive_through(non_owner, lambda event: event.get("type") == "pong")
            no_worker_step = (
                await _matching_count(
                    worker,
                    lambda event: event.get("type") == "control" and event.get("action") == "step",
                )
                == 0
            )
            await _send_control(owner, {"type": "heartbeat"})
            owner_still_current = await _receive_matching(owner, lambda event: event.get("type") == "heartbeat_ack")

    return _observation(
        scenario,
        defaults,
        route="browser_websocket",
        status_code=101,
        non_owner_refused=no_worker_step and owner_still_current["type"] == "heartbeat_ack",
    )


async def _execute_scenario(scenario: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    operation = scenario["input"]["operation"]
    if operation == "fragment_message":
        return await _execute_fragmentation(scenario, defaults)
    if operation == "browser_quota":
        return await _execute_quota(scenario, defaults)
    if operation == "governed_input":
        return await _execute_governance(scenario, defaults)
    if operation == "resume_ownership":
        return await _execute_resume(scenario, defaults)
    if operation == "non_owner_hijack_step":
        return await _execute_non_owner_step(scenario, defaults)
    if operation in {"owner_handoff", "approval_expiry"}:
        if operation == "owner_handoff":
            return await execute_owner_handoff(scenario, defaults)
        return await execute_approval_expiry(scenario, defaults)
    raise AssertionError(f"unknown Python lifecycle operation: {operation}")


def _expected(contract: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    claim = scenario["backends"]["python"]
    return {**contract["result_defaults"], **scenario["expected"], **claim["expected"]}


async def test_python_public_route_session_lifecycle_scenarios() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    scenarios = [item for item in contract["scenarios"] if item["backends"]["python"]["status"] != "unserved"]

    observations = [await _execute_scenario(scenario, contract["result_defaults"]) for scenario in scenarios]

    assert {item["id"] for item in observations} == {item["id"] for item in scenarios}
    if OUTPUT_PATH is None:
        for scenario, observation in zip(scenarios, observations, strict=True):
            expected = _expected(contract, scenario)
            assert observation["status"] == scenario["backends"]["python"]["status"]
            assert {field: observation[field] for field in expected} == expected
    else:
        Path(OUTPUT_PATH).write_text(json.dumps(observations, indent=2) + "\n", encoding="utf-8")
