#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Executable Python adapter for the shared fan-out security scenarios."""

from __future__ import annotations

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
from provide.uterm.server.bridge.hub import EventBus, TermHub
from provide.uterm.server.bridge.hub.ext import PolicyContext, PolicyDecision
from provide.uterm.server.bridge.identity import Principal

ROOT = Path(__file__).resolve().parents[4]
CONTRACT_PATH = Path(os.environ.get("FANOUT_SECURITY_SCENARIO_CONTRACT", ROOT / "spec/fanout_security_scenarios.json"))
OUTPUT_PATH = os.environ.get("FANOUT_SECURITY_SCENARIO_OUTPUT")
ADMIN = Principal(subject_id="admin", roles=frozenset({"admin"}))
ADMIN_HEADERS = {"X-Uterm-Principal": "admin", "X-Uterm-Role": "admin"}


class _Session:
    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id


class ScenarioGate:
    def __init__(self, action: str) -> None:
        self.action = action

    async def intercept_fanout(self, command: str, context: PolicyContext, group_id: str) -> PolicyDecision:
        del command, context, group_id
        return PolicyDecision(action=self.action, reason="denied" if self.action == "deny" else None)  # type: ignore[arg-type]


def _empty(scenario: dict[str, Any], status_code: int, error: str | None) -> dict[str, Any]:
    return {
        "id": scenario["id"],
        "status": scenario["backends"]["python"]["status"],
        "status_code": status_code,
        "error": error,
        "approval_required": False,
        "approval_id": None,
        "delivered_workers": [],
        "observer_notifications": [],
        "failed_members": [],
        "output": {},
    }


def _expected(scenario: dict[str, Any]) -> dict[str, Any]:
    return {**scenario["expected"], **scenario["backends"]["python"]["expected"]}


def _app(*, allow_unknown_members: bool) -> Any:
    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    config.fanout_allow_unknown_members = allow_unknown_members
    config.sessions = []
    return create_server_app(config)


async def _controller(
    members: list[str],
    *,
    readable: set[str] | None = None,
    refused: set[str] | None = None,
    immediate: dict[str, str] | None = None,
    policy: str | None = None,
) -> tuple[FanOutController, TermHub, list[str], list[str]]:
    event_bus = EventBus()
    hub = TermHub(event_bus=event_bus)
    delivered: list[str] = []
    observers: list[str] = []
    refused = refused or set()
    immediate = immediate or {}
    readable = readable if readable is not None else set(members)
    for worker_id in members:
        worker = AsyncMock()
        worker.send_text = AsyncMock()
        await hub.register_worker(worker_id, worker)

    async def send_worker(worker_id: str, message: dict[str, Any], *, source: Any = None) -> bool:
        del message, source
        if worker_id in refused:
            return False
        delivered.append(worker_id)
        await hub.append_event(worker_id, "term", {"data": immediate.get(worker_id, "ok")})
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
    controller = FanOutController(
        hub,
        is_global_admin=is_admin,
        resolve_session=resolve,  # type: ignore[arg-type]
        can_read_session=can_read,  # type: ignore[arg-type]
        fanout_policy_gate=None if policy is None else ScenarioGate(policy),  # type: ignore[arg-type]
    )
    hub.fan_out_controller = controller
    await controller.create_group(
        FanOutGroup(
            group_id="g1",
            name="fleet",
            worker_ids=members,
            created_by="admin",
            created_at=time.time(),
            quiesce_ms=50,
            max_response_ms=500,
        ),
        principal=ADMIN,
    )
    return controller, hub, delivered, observers


def _from_result(
    scenario: dict[str, Any],
    result: FanOutResult,
    delivered: list[str],
    observers: list[str],
    **overrides: Any,
) -> dict[str, Any]:
    observation = {
        **_empty(scenario, 200, result.error),
        "approval_required": result.approval_required,
        "approval_id": "approval" if result.approval_id else None,
        "delivered_workers": delivered,
        "observer_notifications": observers,
        "failed_members": result.failed_sessions,
        "output": {
            entry.worker_id: entry.output_delta
            for entry in result.results
            if entry.ok and entry.output_delta is not None
        },
    }
    observation.update(overrides)
    return observation


def _route_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    scenario_id = scenario["id"]
    if scenario_id == "unauthenticated_refusal":
        response = TestClient(_app(allow_unknown_members=True), raise_server_exceptions=False).post(
            "/api/fanout/groups/missing/send",
            json={"data": "id"},
            headers={"X-Uterm-Principal": "anonymous", "X-Uterm-Role": "viewer"},
        )
        return _empty(scenario, response.status_code, "authentication_required")
    if scenario_id == "viewer_public_session_refusal":
        response = TestClient(_app(allow_unknown_members=True), raise_server_exceptions=False).post(
            "/api/fanout/groups/missing/send",
            json={"data": "id"},
            headers={"X-Uterm-Principal": "viewer", "X-Uterm-Role": "viewer"},
        )
        return _empty(scenario, response.status_code, "global_admin_required")
    permissive = scenario_id == "dormant_member_permissive_admission"
    response = TestClient(_app(allow_unknown_members=permissive), raise_server_exceptions=False).post(
        "/api/fanout/groups",
        json={"name": "dormant", "worker_ids": ["missing"]},
        headers=ADMIN_HEADERS,
    )
    return _empty(scenario, response.status_code, None if response.status_code < 400 else "unknown_member")


async def _execute(scenario: dict[str, Any]) -> dict[str, Any]:
    scenario_id = scenario["id"]
    if scenario_id in {
        "unauthenticated_refusal",
        "viewer_public_session_refusal",
        "dormant_member_default_reject",
        "dormant_member_permissive_admission",
    }:
        return _route_scenario(scenario)
    if scenario_id == "missing_controller_dependencies":
        hub = TermHub(event_bus=EventBus())
        controller = FanOutController(hub)
        await controller.create_group(
            FanOutGroup(group_id="g1", name="fleet", worker_ids=["w1"], created_by="admin", created_at=1),
            principal=ADMIN,
        )
        result = await controller.send("g1", "id", principal=ADMIN)
        return _from_result(scenario, result, [], [], status_code=403, error="authorization_unavailable")
    if scenario_id in {"current_authorization_revocation", "group_grant_non_bypass"}:
        members = ["w1", "w2"] if scenario_id == "current_authorization_revocation" else ["w1"]
        readable = {"w1"} if scenario_id == "current_authorization_revocation" else set()
        controller, _hub, delivered, observers = await _controller(members, readable=readable)
        result = await controller.send("g1", "id", principal=ADMIN)
        return _from_result(scenario, result, delivered, observers)
    if scenario_id == "partial_member_failure":
        controller, _hub, delivered, observers = await _controller(["w1", "w2"], refused={"w2"})
        result = await controller.send("g1", "id", principal=ADMIN)
        return _from_result(scenario, result, delivered, observers)
    if scenario_id == "policy_deny":
        controller, _hub, delivered, observers = await _controller(["w1"], policy="deny")
        result = await controller.send("g1", "rm -rf /", principal=ADMIN)
        return _from_result(scenario, result, delivered, observers, status_code=403, error="policy_denied")
    if scenario_id == "policy_hold_release":
        controller, _hub, delivered, observers = await _controller(["w1"], policy="hold")
        held = await controller.send("g1", "reboot", principal=ADMIN)
        released = await controller.release_approved_command(held.approval_id or "")
        assert released is not None
        return _from_result(
            scenario,
            held,
            delivered,
            observers,
            status_code=202,
            output={
                entry.worker_id: entry.output_delta
                for entry in released.results
                if entry.ok and entry.output_delta is not None
            },
        )
    controller, _hub, delivered, observers = await _controller(["w1"], immediate={"w1": "immediate"})
    result = await controller.send("g1", "id", principal=ADMIN)
    return _from_result(scenario, result, delivered, observers)


async def test_shared_fanout_security_scenarios() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    applicable = [
        scenario for scenario in contract["scenarios"] if scenario["backends"]["python"]["status"] != "unserved"
    ]
    observations = [await _execute(scenario) for scenario in applicable]

    assert {item["id"] for item in observations} == {scenario["id"] for scenario in applicable}
    for scenario, observation in zip(applicable, observations, strict=True):
        assert {key: observation[key] for key in _expected(scenario)} == _expected(scenario)
    if OUTPUT_PATH:
        Path(OUTPUT_PATH).write_text(json.dumps(observations, indent=2) + "\n", encoding="utf-8")
