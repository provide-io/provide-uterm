#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript fan-out routes.

Fan-out is one command typed at many production sessions at once, so its REST
surface is where the blast radius is decided. Three things have to hold, and
each of them is a *quiet* failure when it does not:

* **A group may only contain sessions the creator can already read.** Checked
  at creation, per session, against the sessions that are actually known — an
  unknown worker id is not gated here, and a caller who could not read a
  session must not be able to reach it through a group.
* **Only the creator may delete a group or grant access to it.** Being able to
  *send* to a group is not being able to hand it to somebody else.
* **The status codes are the contract.** 404 before 403, so a caller cannot
  probe for the existence of groups it has no part in; 204 with no body where
  there is nothing to say; 501 when the feature is off, rather than a 500.

The corpus is recorded by calling the *real* route handlers, pulled off the
router the reference registers them on, with a request object standing in for
FastAPI's. What is pinned is therefore the reference's behaviour, defaults
included, rather than a reading of the source.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_fanout_routes_golden.py
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from provide.uterm.server.bridge.fanout import _routes as routes_module
from provide.uterm.server.bridge.fanout._models import FanOutGroup

OUT = Path(__file__).with_name("fanout_routes_golden.json")

PRINCIPAL = "operator@example.org"
OTHER = "intruder@example.org"


class FakeRequest:
    """Stands in for the FastAPI request the handlers read."""

    def __init__(self, principal: str, body: dict[str, Any] | None, *, readable: set[str] | None = None) -> None:
        self.state = SimpleNamespace(uterm_principal=SimpleNamespace(subject_id=principal))
        self._body = {} if body is None else body
        known = {"w1", "w2", "w3", "secret"} if readable is None else readable | {"secret"}
        allowed = {"w1", "w2", "w3"} if readable is None else readable

        async def get_definition(worker_id: str) -> Any:
            return SimpleNamespace(worker_id=worker_id) if worker_id in known else None

        async def can_read_session(_principal: Any, session_def: Any) -> bool:
            return session_def.worker_id in allowed

        # Every route rejects non-admins before doing anything else; the
        # boundary itself is pinned by hand-written tests on both sides, so
        # the corpus records the admin's view of each route's shaping.
        async def is_admin(_principal: Any) -> bool:
            return True

        self.app = SimpleNamespace(
            state=SimpleNamespace(
                uterm_authz=SimpleNamespace(can_read_session=can_read_session, is_admin=is_admin),
                uterm_registry=SimpleNamespace(get_definition=get_definition),
            )
        )

    async def json(self) -> Any:
        """The parsed request body."""
        return self._body


class FakeController:
    """A controller that records what the routes asked of it."""

    def __init__(self) -> None:
        self.groups: dict[str, FanOutGroup] = {}
        self.calls: list[list[Any]] = []
        self.create_error: Exception | None = None
        # Mirrors FakeRequest's default fixtures: w1..w3 readable, "secret"
        # registered but unreadable, anything else unknown.
        self.readable = {"w1", "w2", "w3"}
        self.allow_unknown_members = False
        # The reference refuses outright when a controller cannot judge access
        # at all; the corpus records the routes' own shaping, so this fake is
        # wired.
        self.authorization_ready = True

    async def validate_members(self, worker_ids: list[str], principal: Any) -> tuple[list[str], list[str]]:
        """Split members exactly as the real controller's admission check does."""
        self.calls.append(["validate_members", list(worker_ids), getattr(principal, "subject_id", principal)])
        allowed = [worker_id for worker_id in worker_ids if worker_id in self.readable]
        refused = [worker_id for worker_id in worker_ids if worker_id not in self.readable]
        return allowed, refused

    async def create_group(self, group: FanOutGroup, *, principal: str) -> str:
        """Store the group, or raise what the case asked for."""
        self.calls.append(["create_group", group.group_id, principal])
        if self.create_error is not None:
            raise self.create_error
        self.groups[group.group_id] = group
        return group.group_id

    async def list_groups(self, principal: str) -> list[FanOutGroup]:
        """Every group, so the route's own shaping is what shows."""
        self.calls.append(["list_groups", principal])
        return list(self.groups.values())

    async def get_group(self, group_id: str, *, principal: str) -> FanOutGroup | None:
        """The group, if it is there."""
        self.calls.append(["get_group", group_id, principal])
        return self.groups.get(group_id)

    async def delete_group(self, group_id: str, *, principal: str) -> None:
        """Forget the group."""
        self.calls.append(["delete_group", group_id, principal])
        self.groups.pop(group_id, None)

    async def grant_access(self, group_id: str, grantee: str, *, principal: str) -> None:
        """Record the grant."""
        self.calls.append(["grant_access", group_id, grantee, principal])

    async def send(
        self, group_id: str, data: str, *, principal: str, quiesce_ms: Any = None, max_response_ms: Any = None
    ) -> Any:
        """Return a fixed result, so the route's shaping is what shows."""
        self.calls.append(
            ["send", group_id, data, getattr(principal, "subject_id", principal), quiesce_ms, max_response_ms]
        )
        from provide.uterm.server.bridge.fanout._models import SessionFanOutResult

        return SimpleNamespace(
            group_id=group_id,
            send_id="send-1",
            command=data,
            sent_at=1000.0,
            results=[
                SessionFanOutResult(worker_id="w1", ok=True, output_delta="ok", elapsed_ms=12, divergent=False),
                SessionFanOutResult(worker_id="w2", ok=False, output_delta=None, elapsed_ms=34, divergent=True),
            ],
            divergent_sessions=["w2"],
            failed_sessions=["w2"],
            error=None,
            approval_required=False,
            approval_id=None,
        )


def _handlers(hub: Any) -> dict[str, Any]:
    """Register the real routes and return them by path and method."""
    router = APIRouter()
    routes_module.register_fanout_routes(hub, router)
    return {f"{sorted(route.methods)[0]} {route.path}": route.endpoint for route in router.routes}


async def _call(handler: Any, request: FakeRequest, **kwargs: Any) -> dict[str, Any]:
    """Call a handler and describe the response the way a client sees it."""
    try:
        result = await handler(request, **kwargs)
    except HTTPException as exc:
        return {"status": exc.status_code, "body": {"detail": exc.detail}}
    if isinstance(result, JSONResponse):
        raw = result.body.decode() if result.body else ""
        return {"status": result.status_code, "body": json.loads(raw) if raw else None}
    return {"status": 200, "body": result}


async def _record() -> dict[str, Any]:
    """Drive every route through every branch it has."""
    corpus: dict[str, Any] = {}

    # The feature switched off. A 501 rather than a 500 is the difference
    # between "not built" and "broken".
    disabled = _handlers(SimpleNamespace())
    corpus["disabled"] = {
        name: await _call(handler, FakeRequest(PRINCIPAL, {}), **({"group_id": "g1"} if "{group_id}" in name else {}))
        for name, handler in disabled.items()
    }

    controller = FakeController()
    hub = SimpleNamespace(fan_out_controller=controller)
    handlers = _handlers(hub)
    corpus["routes"] = sorted(handlers)

    audited: list[dict[str, Any]] = []
    real_audit = routes_module.audit_event
    routes_module.audit_event = lambda event, **kwargs: audited.append({"event": event, **kwargs})
    try:
        create = handlers["POST /api/fanout/groups"]
        listing = handlers["GET /api/fanout/groups"]
        delete = handlers["DELETE /api/fanout/groups/{group_id}"]
        send = handlers["POST /api/fanout/groups/{group_id}/send"]
        grant = handlers["POST /api/fanout/groups/{group_id}/grants"]

        # Creation, with everything left to its default.
        bare = await _call(create, FakeRequest(PRINCIPAL, {}))
        created_group = next(iter(controller.groups.values()))
        corpus["create_defaults"] = {
            "response": {**bare, "body": {**bare["body"], "group_id": "<uuid>"}},
            "group": {
                key: value for key, value in asdict(created_group).items() if key not in ("group_id", "created_at")
            },
            "group_id_length": len(created_group.group_id),
            "group_id_is_hex": all(character in "0123456789abcdef" for character in created_group.group_id),
        }
        controller.groups.clear()

        # Creation, with every field given.
        body = {
            "worker_ids": ["w1", "w2"],
            "name": "prod fleet",
            "mode": "sequential",
            "stop_on_first_error": True,
            "error_pattern": "ERROR",
            "quiesce_ms": 100,
            "max_response_ms": 2000,
            "divergence_threshold": 0.5,
        }
        full = await _call(create, FakeRequest(PRINCIPAL, body))
        created_group = next(iter(controller.groups.values()))
        corpus["create_full"] = {
            "response": {**full, "body": {**full["body"], "group_id": "<uuid>"}},
            "group": {
                key: value for key, value in asdict(created_group).items() if key not in ("group_id", "created_at")
            },
        }
        group_id = created_group.group_id

        # A session the caller cannot read, and an unknown one, which is not
        # gated here at all.
        corpus["create_forbidden"] = await _call(
            create, FakeRequest(PRINCIPAL, {"worker_ids": ["w1", "secret"]}, readable={"w1", "w2", "w3"})
        )
        unknown = await _call(create, FakeRequest(PRINCIPAL, {"worker_ids": ["never-registered"]}))
        corpus["create_unknown_session"] = {**unknown, "body": {**unknown["body"], "group_id": "<uuid>"}}

        # A controller that cannot judge access at all. It does not get to
        # admit members on the strength of the checks that remain, so this is
        # refused before any member is looked at.
        controller.authorization_ready = False
        corpus["create_authorization_unavailable"] = await _call(create, FakeRequest(PRINCIPAL, {"worker_ids": ["w1"]}))
        controller.authorization_ready = True

        # A controller that refuses the group.
        controller.create_error = ValueError("group too large: 99 > 50")
        corpus["create_rejected"] = await _call(create, FakeRequest(PRINCIPAL, {"worker_ids": ["w1"]}))
        controller.create_error = None

        # A body that is not an object at all. The reference reaches for
        # `.get` on it, so this is not a 400 — it is an unhandled error, and a
        # port that quietly treated it as an empty object would accept a
        # request the reference rejects.
        malformed = {}
        for name, handler, kwargs in (
            ("create", create, {}),
            ("send", send, {"group_id": "g-none"}),
            ("grant", grant, {"group_id": "g-none"}),
        ):
            request_with_list = FakeRequest(PRINCIPAL, None)
            request_with_list._body = ["not", "an", "object"]
            if name != "create":
                controller.groups["g-none"] = FanOutGroup(
                    group_id="g-none", name="", worker_ids=[], created_by=PRINCIPAL, created_at=1.0
                )
            try:
                await _call(handler, request_with_list, **kwargs)
            except Exception as exc:  # recording what escapes is the point
                malformed[name] = type(exc).__name__
            else:
                malformed[name] = None
        controller.groups.pop("g-none", None)
        corpus["malformed_body"] = malformed

        corpus["list"] = await _call(listing, FakeRequest(PRINCIPAL, None))

        # Reads and writes against a group that is not there, and one the
        # caller did not create.
        corpus["missing"] = {
            "delete": await _call(delete, FakeRequest(PRINCIPAL, None), group_id="nope"),
            "send": await _call(send, FakeRequest(PRINCIPAL, {"data": "ls\\r"}), group_id="nope"),
            "grant": await _call(grant, FakeRequest(PRINCIPAL, {"grantee": OTHER}), group_id="nope"),
        }
        corpus["not_creator"] = {
            "delete": await _call(delete, FakeRequest(OTHER, None), group_id=group_id),
            "grant": await _call(grant, FakeRequest(OTHER, {"grantee": OTHER}), group_id=group_id),
        }

        # A send, which is where the result shaping lives.
        corpus["send"] = await _call(
            send,
            FakeRequest(PRINCIPAL, {"data": "uptime\\r", "quiesce_ms": 50, "max_response_ms": 900}),
            group_id=group_id,
        )
        corpus["send_defaults_call"] = None
        await _call(send, FakeRequest(PRINCIPAL, {}), group_id=group_id)
        corpus["send_defaults_call"] = controller.calls[-1]

        # The grant and the delete, in that order: the delete removes the
        # group the grant needs.
        corpus["grant"] = await _call(grant, FakeRequest(PRINCIPAL, {"grantee": OTHER}), group_id=group_id)
        corpus["grant_default_call"] = None
        await _call(grant, FakeRequest(PRINCIPAL, {}), group_id=group_id)
        corpus["grant_default_call"] = controller.calls[-1]
        corpus["delete"] = await _call(delete, FakeRequest(PRINCIPAL, None), group_id=group_id)
        corpus["delete_removed_it"] = group_id not in controller.groups

        # A command long enough to be truncated in the audit record.
        long_group = FanOutGroup(
            group_id="g-long",
            name="",
            worker_ids=["w1"],
            created_by=PRINCIPAL,
            created_at=1.0,
        )
        controller.groups["g-long"] = long_group
        await _call(send, FakeRequest(PRINCIPAL, {"data": "x" * 300}), group_id="g-long")
        corpus["audit"] = [
            {**record, "detail": {**record["detail"], "group_id": "<uuid>"}}
            if "group_id" in record.get("detail", {})
            else record
            for record in audited
        ]
        corpus["audit_command_length"] = len(audited[-1]["detail"]["command"])
        corpus["_group_id"] = group_id
    finally:
        routes_module.audit_event = real_audit

    return corpus


def _stable(value: Any, group_id: str) -> Any:
    """Replace every generated identifier so the corpus does not drift.

    A `uuid4` is different on every run, so leaving one in would make the
    drift gate fail against itself and say nothing about the reference.
    """
    if isinstance(value, str):
        looks_generated = len(value) == 32 and all(character in "0123456789abcdef" for character in value)
        return "<uuid>" if value == group_id or looks_generated else value
    if isinstance(value, list):
        return [_stable(item, group_id) for item in value]
    if isinstance(value, dict):
        return {key: _stable(item, group_id) for key, item in value.items()}
    return value


async def _main() -> int:
    """Write the golden corpus and report the case count."""
    corpus = await _record()
    corpus = _stable(corpus, corpus.pop("_group_id"))
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(corpus['routes'])} routes, {len(corpus['audit'])} audit records)")
    return 0


def main() -> int:
    """Entry point."""
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
