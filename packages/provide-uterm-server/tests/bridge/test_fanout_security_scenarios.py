#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Data-driven Python interpreter for the shared fan-out security contract."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.bridge.fanout._controller import FanOutController
from provide.uterm.server.bridge.fanout._models import FanOutGroup, FanOutResult
from provide.uterm.server.bridge.fanout._store import InMemoryFanOutStore
from provide.uterm.server.bridge.hub import EventBus, TermHub
from provide.uterm.server.bridge.hub.ext import PolicyContext, PolicyDecision
from provide.uterm.server.bridge.identity import Principal
from provide.uterm.server.config_schema_session import SessionDefinition

ROOT = Path(__file__).resolve().parents[4]
CONTRACT_PATH = Path(os.environ.get("FANOUT_SECURITY_SCENARIO_CONTRACT", ROOT / "spec/fanout_security_scenarios.json"))
OUTPUT_PATH = os.environ.get("FANOUT_SECURITY_SCENARIO_OUTPUT")

# The response budget for scenarios that do not ask for one.
#
# Nineteen of the twenty scenarios test authorization semantics -- who may send,
# who is refused, who is notified. Exactly one, ``total_response_deadline``,
# tests the deadline itself, and it names its own 20ms. So this default is
# incidental to every scenario that inherits it, and it must be large enough
# that the clock never decides their outcome.
#
# 100ms was not. A member whose budget expires is reported in
# ``failed_members`` -- deliberately: that is what ``total_response_deadline``
# asserts. Under load the C# port reached that state on a member that was
# authorized and delivered to, turning
# ``current_authorization_revocation`` into ``failed_members: [w1, w2]``
# against an expected ``[w2]``. Reproduced exactly by shrinking this number:
# 40ms passes, 30ms and 20ms produce that failure verbatim, 1ms fails earlier
# still with nothing delivered at all.
#
# Costs nothing. A collect returns once output has been quiet for quiesce_ms,
# not when the budget runs out, so raising the ceiling does not slow a scenario
# that behaves. Only ``continuous_output`` runs to the deadline, and that one
# sets its own.
DEFAULT_MAX_RESPONSE_MS = 5_000


class _Session:
    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id


class _Gate:
    def __init__(self, action: str) -> None:
        self.action = action

    async def intercept_fanout(self, command: str, context: PolicyContext, group_id: str) -> PolicyDecision:
        del command, context, group_id
        if self.action == "deny":
            return PolicyDecision(action="deny", reason="policy_denied")
        if self.action == "hold_release":
            return PolicyDecision(action="hold")
        return PolicyDecision(action="allow")


def _principal(actor: dict[str, Any]) -> Principal | None:
    if not actor["authenticated"]:
        return None
    return Principal(subject_id=actor["subject"], roles=frozenset(actor["roles"]))


def _headers(actor: dict[str, Any]) -> dict[str, str]:
    return {
        "X-Uterm-Principal": actor["subject"],
        "X-Uterm-Role": actor["roles"][0] if actor["roles"] else "viewer",
    }


def _canonical_route_error(status: int, body: Any) -> str | None:
    if status < 400:
        return None
    message = ""
    if isinstance(body, dict):
        message = str(body.get("error", body.get("detail", "")))
    if status == 401:
        return "authentication_required"
    if "admin" in message:
        return "global_admin_required"
    if "unknown fan-out" in message:
        return "unknown_member"
    if "no read access" in message:
        return "member_read_forbidden"
    if "authorization" in message:
        return "authorization_unavailable"
    return message or "request_failed"


def _base(scenario: dict[str, Any], status: int, command: str, error: str | None) -> dict[str, Any]:
    return {
        "id": scenario["id"],
        "status": scenario["backends"]["python"]["status"],
        "status_code": status,
        "error": error,
        "approval_required": False,
        "approval_id": None,
        "command": command,
        "delivered_workers": [],
        "observer_notifications": [],
        "failed_members": [],
        "output": {},
    }


def _from_result(
    scenario: dict[str, Any], result: FanOutResult, delivered: list[str], observers: list[str], status: int = 200
) -> dict[str, Any]:
    return {
        **_base(scenario, status, result.command, result.error),
        "approval_required": result.approval_required,
        "approval_id": "approval" if result.approval_id else None,
        "delivered_workers": list(delivered),
        "observer_notifications": list(observers),
        "failed_members": list(result.failed_sessions),
        "output": {
            row.worker_id: row.output_delta for row in result.results if row.ok and row.output_delta is not None
        },
    }


async def _build(
    input_data: dict[str, Any], hub: TermHub | None = None
) -> tuple[FanOutController, TermHub, list[str], list[str]]:
    hub = hub or TermHub(event_bus=EventBus())
    delivered: list[str] = []
    observers: list[str] = []
    workers = input_data["workers"]
    accepted = set(workers["accepted_members"])
    immediate = workers["immediate_output"]
    # A fixture may give the controller a wider view than the server has, to
    # prove that admission still follows the server's answer.
    visibility = input_data["visibility"]
    readable = set(visibility.get("controller_readable_members", visibility["readable_members"]))
    readable.difference_update(visibility["revoke_before_send"])
    for worker_id in input_data["group"]["members"]:
        worker = AsyncMock()
        worker.send_text = AsyncMock()
        await hub.register_worker(worker_id, worker)

    continuous = bool(workers.get("continuous_output", False))
    budget_s = input_data.get("max_response_ms", DEFAULT_MAX_RESPONSE_MS) / 1000.0

    async def _never_falls_quiet(worker_id: str) -> None:
        """Emit output the way ``tail -f`` does, so the collect can only end on its budget.

        ``quiesce_ms`` is 1 here, so a member that pauses even briefly ends the
        collect as quiesced rather than cut short.

        A BATCH per turn, not one event. The collector consumes one event per
        iteration and yields, so a producer that appends one and yields leaves
        the queue oscillating between empty and one -- and truncation is read
        off what is still queued at exit, so an exit landing on the empty half
        reports a member that never stopped talking as complete. This is the
        single-threaded analogue of the four producer goroutines go's harness
        needs for the same reason. The subscription's byte cap bounds the
        backlog, so a batch saturates the queue rather than growing it.

        Self-limiting rather than cancelled by the caller: it outlives the
        budget by a wide margin and then stops on its own, so no scenario can
        leak a spinning task into the ones that run after it.
        """
        stop_at = time.monotonic() + (budget_s * 5) + 0.05
        while time.monotonic() < stop_at:
            for _ in range(32):
                await hub.append_event(worker_id, "term", {"data": "."})
            await asyncio.sleep(0)

    async def send_worker(worker_id: str, message: dict[str, Any], *, source: Any = None) -> bool:
        del message, source
        if worker_id not in accepted:
            return False
        delivered.append(worker_id)
        await hub.append_event(worker_id, "term", {"data": immediate.get(worker_id, "ok")})
        if continuous:
            asyncio.get_running_loop().create_task(_never_falls_quiet(worker_id))
        return True

    async def broadcast(worker_id: str, message: dict[str, Any]) -> None:
        if message.get("type") == "fanout_input":
            observers.append(worker_id)

    async def is_admin(principal: Principal) -> bool:
        return "admin" in principal.roles and principal.admin_session_scope is None

    async def resolve(worker_id: str) -> _Session:
        return _Session(worker_id)

    async def can_read(principal: Principal, definition: _Session) -> bool:
        del principal
        return definition.worker_id in readable

    hub.send_worker = send_worker  # type: ignore[method-assign]
    hub.broadcast = broadcast  # type: ignore[method-assign]
    policy = input_data["policy"]["action"]
    controller = FanOutController(
        hub,
        is_global_admin=None if input_data.get("omit_authorizers") else is_admin,
        resolve_session=None if input_data.get("omit_authorizers") else resolve,  # type: ignore[arg-type]
        can_read_session=None if input_data.get("omit_authorizers") else can_read,  # type: ignore[arg-type]
        fanout_policy_gate=_Gate(policy),  # type: ignore[arg-type]
        allow_unknown_members=input_data["group"]["allow_unknown_members"],
    )
    hub.fan_out_controller = controller
    group_data = input_data["group"]
    creator = Principal(subject_id=group_data["creator"], roles=frozenset({"admin"}))
    await controller.create_group(
        FanOutGroup(
            group_id=group_data["id"],
            name="fixture-group",
            worker_ids=list(group_data["members"]),
            created_by=group_data["creator"],
            created_at=time.time(),
            grants=list(group_data["grants"]),
            quiesce_ms=1,
            max_response_ms=input_data.get("max_response_ms", DEFAULT_MAX_RESPONSE_MS),
        ),
        principal=creator,
    )
    return controller, hub, delivered, observers


class _FixtureAuthz:
    """The server's authorizer, with the fixture answering read access.

    The real service is slotted, so its decision is replaced by wrapping rather
    than by assignment; everything the routes ask of it other than read access
    still goes to the real one.
    """

    def __init__(self, inner: Any, readable: set[str]) -> None:
        self._inner = inner
        self._readable = readable

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def can_read_session(self, principal: Principal, definition: Any) -> bool:
        del principal
        return definition.session_id in self._readable


def _registered_members(input_data: dict[str, Any]) -> list[str]:
    """The members the session registry knows about.

    A readable session must exist to be readable, so the visible set is the
    floor; a fixture naming ``registered_members`` adds the sessions that exist
    without being readable, which is what separates an unknown member from a
    forbidden one.
    """
    visibility = input_data["visibility"]
    registered = list(visibility.get("registered_members", visibility["readable_members"]))
    for worker_id in visibility["readable_members"]:
        if worker_id not in registered:
            registered.append(worker_id)
    return registered


def _make_app(allow_unknown_members: bool, registered: list[str]) -> Any:
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    config.fanout_allow_unknown_members = allow_unknown_members
    config.sessions = [SessionDefinition(session_id=worker_id, auto_start=False) for worker_id in registered]
    return create_server_app(config)


async def _execute_rest(scenario: dict[str, Any]) -> dict[str, Any]:
    input_data = scenario["input"]
    app = _make_app(input_data["group"]["allow_unknown_members"], _registered_members(input_data))
    controller, _hub, delivered, observers = await _build(input_data, app.state.uterm_hub)
    app.state.uterm_hub.fan_out_controller = controller

    # The route reads access off the same authorizer the rest of the server
    # does, so the fixture's visible set has to answer there too — otherwise a
    # registered member is readable purely because the actor is an admin.
    readable = set(input_data["visibility"]["readable_members"])
    readable.difference_update(input_data["visibility"]["revoke_before_send"])

    app.state.uterm_authz = _FixtureAuthz(app.state.uterm_authz, readable)
    with TestClient(app, raise_server_exceptions=False) as client:
        if input_data["operation"] == "create":
            response = client.post(
                "/api/fanout/groups",
                json={"name": "fixture-group", "worker_ids": input_data["group"]["members"]},
                headers=_headers(input_data["actor"]),
            )
            body = response.json()
            return _base(
                scenario,
                response.status_code,
                input_data["command"],
                _canonical_route_error(response.status_code, body),
            )
        response = client.post(
            f"/api/fanout/groups/{input_data['group']['id']}/send",
            json={"data": input_data["command"], "max_response_ms": input_data.get("max_response_ms")},
            headers=_headers(input_data["actor"]),
        )
        body = response.json()
        # Release while the app lifespan is still active. Hub shutdown now
        # synchronously closes both event generations, so releasing after the
        # TestClient context exits correctly has no operation-output stream.
        if input_data["surface"] == "rest_release" and response.status_code == 200 and body.get("approval_id"):
            approval = _hub.approval_store.get(body["approval_id"])
            assert approval is not None
            released = await controller.release_approved_command(
                body["approval_id"],
                expected_revision=approval.revision,
            )
            assert released is not None
            observation = _from_result(scenario, released, delivered, observers, response.status_code)
            observation["approval_required"] = body["approval_required"]
            observation["approval_id"] = "approval"
            return observation
    if response.status_code != 200 or "results" not in body:
        return _base(
            scenario,
            response.status_code,
            input_data["command"],
            _canonical_route_error(response.status_code, body),
        )
    held = FanOutResult(
        group_id=body["group_id"],
        send_id=body["send_id"],
        command=body["command"],
        sent_at=body["sent_at"],
        results=[],
        divergent_sessions=body["divergent_sessions"],
        failed_sessions=body["failed_sessions"],
        error=body["error"],
        approval_required=body["approval_required"],
        approval_id=body["approval_id"],
    )
    if input_data["surface"] == "rest_release" and held.approval_id:
        approval = _hub.approval_store.get(held.approval_id)
        assert approval is not None
        released = await controller.release_approved_command(
            held.approval_id,
            expected_revision=approval.revision,
        )
        assert released is not None
        observation = _from_result(scenario, released, delivered, observers, response.status_code)
        observation["approval_required"] = held.approval_required
        observation["approval_id"] = "approval"
        return observation
    return _from_result(scenario, held, delivered, observers, response.status_code)


async def _execute_controller(scenario: dict[str, Any]) -> dict[str, Any]:
    input_data = scenario["input"]
    controller, _hub, delivered, observers = await _build(input_data)
    result = await controller.send(
        input_data["group"]["id"],
        input_data["command"],
        principal=_principal(input_data["actor"]),
        max_response_ms=input_data.get("max_response_ms"),
    )
    status = 403 if result.error and "authorization" in result.error else 200
    observation = _from_result(scenario, result, delivered, observers, status)
    if result.error and "authorization" in result.error:
        observation["error"] = "authorization_unavailable"
    return observation


async def _execute_store(scenario: dict[str, Any]) -> dict[str, Any]:
    """Exercise the real store boundary and reject every mutable alias."""
    input_data = scenario["input"]
    group_data = input_data["group"]
    store = InMemoryFanOutStore()
    original = FanOutGroup(
        group_id=group_data["id"],
        name="fixture-group",
        worker_ids=list(group_data["members"]),
        created_by=group_data["creator"],
        created_at=1,
        grants=list(group_data["grants"]),
    )
    await store.save(original)
    if input_data["operation"] == "store_atomic_update":
        grants = input_data["concurrent_grants"]
        await asyncio.gather(*(store.grant_access(group_data["id"], grant, group_data["creator"]) for grant in grants))
        persisted = await store.get(group_data["id"])
        assert persisted is not None
        assert set(persisted.grants) == set(group_data["grants"]) | set(grants)
        return _base(scenario, 200, input_data["command"], None)
    mutation = input_data["mutation_member"]
    original.worker_ids.append(mutation)
    original.created_by = "mutated-creator"
    original.grants.append("mutated-grant")
    fetched = await store.get(group_data["id"])
    assert fetched is not None
    assert fetched.worker_ids == group_data["members"]
    assert fetched.created_by == group_data["creator"]
    assert fetched.grants == group_data["grants"]
    fetched.worker_ids.append(mutation)
    fetched.grants.append("mutated-read")
    listed = await store.list_for_principal(group_data["creator"])
    assert len(listed) == 1
    listed[0].created_by = "mutated-list"
    listed[0].worker_ids.append(mutation)
    persisted = await store.get(group_data["id"])
    assert persisted is not None
    assert persisted.worker_ids == group_data["members"]
    assert persisted.created_by == group_data["creator"]
    assert persisted.grants == group_data["grants"]
    return _base(scenario, 200, input_data["command"], None)


async def _execute(scenario: dict[str, Any]) -> dict[str, Any]:
    surface = scenario["input"]["surface"]
    if surface in {"rest", "rest_release"}:
        return await _execute_rest(scenario)
    if surface == "controller":
        return await _execute_controller(scenario)
    if surface == "store":
        return await _execute_store(scenario)
    raise AssertionError(f"Python backend does not serve {surface}")


async def test_shared_fanout_security_scenarios() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    applicable = [item for item in contract["scenarios"] if item["backends"]["python"]["status"] != "unserved"]
    observations = [await _execute(scenario) for scenario in applicable]
    assert {item["id"] for item in observations} == {item["id"] for item in applicable}
    if OUTPUT_PATH is None:
        for scenario, observation in zip(applicable, observations, strict=True):
            expected = {**scenario["expected"], **scenario["backends"]["python"]["expected"]}
            assert {key: observation[key] for key in expected} == expected
    else:
        Path(OUTPUT_PATH).write_text(json.dumps(observations, indent=2) + "\n", encoding="utf-8")
