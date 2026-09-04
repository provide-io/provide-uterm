#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the ``/gui/`` REST surface.

These six handlers are how an agent reaches somebody's screen, so what is
recorded is every refusal as well as every success:

* **Attaching** needs the capability, a target the caller's tenant may see,
  and a protocol this system speaks. Each is a different status, and the
  order matters — a caller that learns "not found" versus "forbidden" learns
  which targets another tenant has.
* **Injecting** is bound to the principal who took the lease, not merely to
  whoever holds the hijack id. Possession of an unguessable string is not the
  same as being the one who asked for it.
* **What a body may say.** An integer field written as a string is taken, a
  boolean is not, and an unreadable body is an empty one rather than an
  error — so the coercion is recorded field by field.

The handlers are driven for real, with the same fakes the reference's own
tests use, and what is recorded is the status, the body, and what the console
looked like afterwards.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_guiroutes_golden.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from provide.uterm.server.bridge.hub.registry import WorkerRegistry
from provide.uterm.server.bridge.models import WorkerTermState
from provide.uterm.server.bridge.routes import rest_gui
from provide.uterm.server.graphical_targets import GraphicalTargetDefinition, InMemoryGraphicalTargetRegistry
from provide.uterm.server.gui_session import MemoryGraphicalSession

OUT = Path(__file__).resolve().parent / "guiroutes_golden.json"

WORKER = "gui-worker"
HIJACK = "00000000-0000-0000-0000-000000000000"

ATTACH = "/worker/{worker_id}/gui/attach"
SHOT = "/worker/{worker_id}/hijack/{hijack_id}/gui/screenshot"
CLICK = "/worker/{worker_id}/hijack/{hijack_id}/gui/click"
TYPE = "/worker/{worker_id}/hijack/{hijack_id}/gui/type"
KEY = "/worker/{worker_id}/hijack/{hijack_id}/gui/key"
DRAG = "/worker/{worker_id}/hijack/{hijack_id}/gui/drag"

# The console every injection scenario draws on, small enough to read.
CONSOLE = (6, 4)


class _Request:
    """As much of a Starlette request as these handlers touch."""

    def __init__(self, *, principal: Any, authz: Any, targets: Any, body: Any, bad_json: bool) -> None:
        self.state = SimpleNamespace(uterm_principal=principal)
        self.app = SimpleNamespace(state=SimpleNamespace(uterm_authz=authz, uterm_graphical_targets=targets))
        self._body = body
        self._bad_json = bad_json

    async def json(self) -> Any:
        if self._bad_json:
            raise ValueError("not json")
        return self._body


class _Recorder(MemoryGraphicalSession):
    """A console that draws as the stub does and remembers what it was told.

    Which is the part that matters for keys: a memory console swallows them,
    so without this a typed word and a silence look the same.
    """

    def __init__(self, width: int, height: int) -> None:
        super().__init__(width, height)
        self.calls: list[list[Any]] = []

    def inject_pointer(self, x: int, y: int, button_mask: int) -> None:
        self.calls.append(["pointer", x, y, button_mask])
        super().inject_pointer(x, y, button_mask)

    def inject_key(self, key_sym: int, down: bool) -> None:
        self.calls.append(["key", key_sym, down])
        super().inject_key(key_sym, down)


class _Authz:
    def __init__(self, *, allow: bool) -> None:
        self._allow = allow

    async def has_capability(self, principal: Any, capability: str) -> bool:
        _ = (principal, capability)
        return self._allow


class _Lease:
    def __init__(self, acquired_by: str | None) -> None:
        self.lease_expires_at = 1000.0
        self.acquired_by = acquired_by


def _targets() -> InMemoryGraphicalTargetRegistry:
    registry = InMemoryGraphicalTargetRegistry()
    registry.add_static(
        GraphicalTargetDefinition(
            target_id="gt-mem", tenant_id="acme", protocol="memory", width=CONSOLE[0], height=CONSOLE[1]
        )
    )
    registry.add_static(
        # litevirt, not rfb: this case records "a protocol this system does not
        # speak", and Python speaks rfb now (server/rfb_session.py). litevirt is
        # Go's console protocol — neither the reference nor the TypeScript port
        # implements it, so the case keeps meaning what its name says. Note the
        # ports' gaps are deliberate mirrors: Go 501s rfb, C# 501s litevirt.
        GraphicalTargetDefinition(
            target_id="gt-litevirt", tenant_id="acme", protocol="litevirt", endpoint="1.2.3.4:5900"
        )
    )
    registry.add_static(
        GraphicalTargetDefinition(target_id="gt-other", tenant_id="other", protocol="memory", width=2, height=2)
    )
    return registry


def _endpoint(hub: Any, path: str) -> Any:
    router = APIRouter()
    rest_gui.register_gui_routes(hub, router)
    for route in router.routes:
        if getattr(route, "path", "") == path:
            return route.endpoint
    raise AssertionError(f"route not found: {path}")


def _answer(result: Any) -> dict[str, Any]:
    """A handler's answer as a status and a body.

    Called ``response`` rather than ``body``, which is what the *request*
    carries: recording both under one name would have quietly thrown the
    inputs away.
    """
    if isinstance(result, JSONResponse):
        return {"status": result.status_code, "response": json.loads(result.body)}
    return {"status": 200, "response": result}


# (name, path, tenant, allow, body, bad_json, has_lease, acquired_by, attached, subject)
CASES: list[dict[str, Any]] = [
    # --- attaching -------------------------------------------------------
    {"name": "attaching without the capability", "path": ATTACH, "allow": False, "body": {"target_id": "gt-mem"}},
    {"name": "attaching to nothing named", "path": ATTACH, "body": {}},
    {"name": "attaching to a name of spaces", "path": ATTACH, "body": {"target_id": "   "}},
    {"name": "attaching to a name that is not text", "path": ATTACH, "body": {"target_id": 7}},
    {"name": "attaching with an unreadable body", "path": ATTACH, "bad_json": True},
    {"name": "attaching with a body that is a list", "path": ATTACH, "body": [1, 2]},
    {"name": "attaching with no tenant at all", "path": ATTACH, "tenant": None, "body": {"target_id": "gt-mem"}},
    {"name": "attaching with a blank tenant", "path": ATTACH, "tenant": "  ", "body": {"target_id": "gt-mem"}},
    {"name": "attaching to a target nobody has", "path": ATTACH, "body": {"target_id": "absent"}},
    {"name": "attaching to another tenant's target", "path": ATTACH, "body": {"target_id": "gt-other"}},
    {"name": "attaching to a protocol nobody speaks yet", "path": ATTACH, "body": {"target_id": "gt-litevirt"}},
    {"name": "attaching to a console", "path": ATTACH, "body": {"target_id": "gt-mem"}},
    {"name": "attaching with the name padded", "path": ATTACH, "body": {"target_id": "  gt-mem  "}},
    {
        "name": "attaching when the worker is already known",
        "path": ATTACH,
        "body": {"target_id": "gt-mem"},
        "known_worker": True,
    },
    # --- the screenshot ---------------------------------------------------
    {"name": "a screenshot with no lease", "path": SHOT, "has_lease": False},
    {"name": "a screenshot with no console", "path": SHOT},
    {"name": "a screenshot", "path": SHOT, "attached": True},
    {"name": "a screenshot of a console somebody drew on", "path": SHOT, "attached": True, "drawn": [(1, 1)]},
    # --- clicking ---------------------------------------------------------
    {"name": "clicking with no lease", "path": CLICK, "has_lease": False, "body": {"x": 1, "y": 1}},
    {"name": "clicking with no console", "path": CLICK, "body": {"x": 1, "y": 1}},
    {
        "name": "clicking somebody else's lease",
        "path": CLICK,
        "attached": True,
        "acquired_by": "somebody",
        "body": {"x": 1, "y": 1},
    },
    {
        "name": "clicking a lease nobody claimed",
        "path": CLICK,
        "attached": True,
        "acquired_by": None,
        "body": {"x": 1, "y": 1},
    },
    {
        "name": "clicking one's own lease",
        "path": CLICK,
        "attached": True,
        "acquired_by": "u1",
        "body": {"x": 1, "y": 1},
    },
    {"name": "clicking", "path": CLICK, "attached": True, "body": {"x": 2, "y": 1}},
    {"name": "clicking with no coordinates", "path": CLICK, "attached": True, "body": {}},
    {
        "name": "clicking the middle button",
        "path": CLICK,
        "attached": True,
        "body": {"x": 1, "y": 1, "button": "middle"},
    },
    {"name": "clicking the right button", "path": CLICK, "attached": True, "body": {"x": 1, "y": 1, "button": "right"}},
    {"name": "clicking a button nobody has", "path": CLICK, "attached": True, "body": {"x": 1, "y": 1, "button": "up"}},
    {
        "name": "clicking a button in capitals",
        "path": CLICK,
        "attached": True,
        "body": {"x": 1, "y": 1, "button": "LEFT"},
    },
    {"name": "clicking with a button that is not text", "path": CLICK, "attached": True, "body": {"button": 1}},
    {"name": "clicking at coordinates written as text", "path": CLICK, "attached": True, "body": {"x": "2", "y": "1"}},
    {
        "name": "clicking at coordinates padded with spaces",
        "path": CLICK,
        "attached": True,
        "body": {"x": " 2 ", "y": "1"},
    },
    {
        "name": "clicking at coordinates that are not numbers",
        "path": CLICK,
        "attached": True,
        "body": {"x": "two", "y": 1},
    },
    {"name": "clicking at a coordinate that is true", "path": CLICK, "attached": True, "body": {"x": True, "y": 1}},
    {"name": "clicking at a fractional coordinate", "path": CLICK, "attached": True, "body": {"x": 1.9, "y": 1}},
    {"name": "clicking off the console", "path": CLICK, "attached": True, "body": {"x": 99, "y": 99}},
    {"name": "clicking with an unreadable body", "path": CLICK, "attached": True, "bad_json": True},
    # --- typing -----------------------------------------------------------
    {"name": "typing with no lease", "path": TYPE, "has_lease": False, "body": {"text": "hi"}},
    {"name": "typing with no console", "path": TYPE, "body": {"text": "hi"}},
    {"name": "typing", "path": TYPE, "attached": True, "body": {"text": "hi"}},
    {"name": "typing nothing", "path": TYPE, "attached": True, "body": {"text": ""}},
    {"name": "typing something that is not text", "path": TYPE, "attached": True, "body": {"text": 7}},
    {"name": "typing past the basic plane", "path": TYPE, "attached": True, "body": {"text": "aé\U0001f600"}},
    # --- named keys -------------------------------------------------------
    {"name": "a key with no lease", "path": KEY, "has_lease": False, "body": {"key_name": "Enter"}},
    {"name": "a key with no console", "path": KEY, "body": {"key_name": "Enter"}},
    {"name": "a key", "path": KEY, "attached": True, "body": {"key_name": "Enter"}},
    {"name": "a key nobody named", "path": KEY, "attached": True, "body": {}},
    {"name": "a key nobody has", "path": KEY, "attached": True, "body": {"key_name": "F13"}},
    {"name": "a key in the wrong case", "path": KEY, "attached": True, "body": {"key_name": "enter"}},
    # --- dragging ---------------------------------------------------------
    {"name": "a drag with no lease", "path": DRAG, "has_lease": False, "body": {}},
    {"name": "a drag with no console", "path": DRAG, "body": {}},
    {
        "name": "a drag",
        "path": DRAG,
        "attached": True,
        "body": {"start_x": 0, "start_y": 0, "end_x": 3, "end_y": 2},
    },
    {"name": "a drag from nowhere to nowhere", "path": DRAG, "attached": True, "body": {}},
    {
        "name": "a drag written as text",
        "path": DRAG,
        "attached": True,
        "body": {"start_x": "1", "start_y": "1", "end_x": "2", "end_y": "2"},
    },
    {
        "name": "a drag off the console",
        "path": DRAG,
        "attached": True,
        "body": {"start_x": 1, "start_y": 1, "end_x": 99, "end_y": 99},
    },
]

# Body values fed to the two field readers, which decide what a caller may say.
INT_FIELDS: list[Any] = [7, -3, 0, "7", " 7 ", "-3", "", "seven", "7.5", 7.5, True, False, None, [7], {"a": 1}]
STR_FIELDS: list[Any] = ["hi", "", "  hi  ", 7, 7.5, True, None, ["hi"], {"a": 1}]


async def _run(case: dict[str, Any]) -> dict[str, Any]:
    tenant = case.get("tenant", "acme")
    principal = SimpleNamespace(tenant_id=tenant, subject_id=case.get("subject", "u1"))

    registry = WorkerRegistry()
    console: _Recorder | None = None
    if case.get("attached") or case.get("known_worker"):
        state = WorkerTermState()
        if case.get("attached"):
            console = _Recorder(*CONSOLE)
            for x, y in case.get("drawn", []):
                console.inject_pointer(x, y, 1)
            console.calls.clear()
            state.graphical_session = console
        registry.put(WORKER, state)

    lease = None if case.get("has_lease") is False else _Lease(case.get("acquired_by"))

    async def get_rest_session(worker_id: str, hijack_id: str) -> Any:
        _ = (worker_id, hijack_id)
        return lease

    hub = SimpleNamespace(registry=registry, get_rest_session=get_rest_session)
    request = _Request(
        principal=principal,
        authz=_Authz(allow=case.get("allow", True)),
        targets=_targets(),
        body=case.get("body", {}),
        bad_json=case.get("bad_json", False),
    )

    handler = _endpoint(hub, case["path"])
    if case["path"] == SHOT:
        answer = _answer(await handler(worker_id=WORKER, hijack_id=HIJACK))
    elif case["path"] == ATTACH:
        answer = _answer(await handler(request, worker_id=WORKER))
    else:
        answer = _answer(await handler(request, worker_id=WORKER, hijack_id=HIJACK))

    # A screenshot carries a lease expiry read off two clocks, so it says only
    # that there was one; what the conversion does is recorded separately.
    response = answer["response"]
    if isinstance(response, dict) and "lease_expires_at" in response:
        response["lease_expires_at"] = "<a time>"

    after = registry.get(WORKER)
    attached = None if after is None else after.graphical_session
    return {
        **answer,
        "calls": [] if console is None else console.calls,
        "console": None if attached is None else base64.b64encode(bytes(attached.screenshot().pixels)).decode("ascii"),
        "lit": []
        if attached is None
        else [
            index // 4
            for index in range(0, len(attached.screenshot().pixels), 4)
            if attached.screenshot().pixels[index] == 255
        ],
    }


def _mono_to_wall_samples() -> list[dict[str, float]]:
    """What the monotonic-to-wall conversion does, with both clocks held still.

    A lease expiry is read off a clock that only counts forward from an
    arbitrary zero, and what a client is told is a wall-clock instant.
    """
    real_time, real_monotonic = time.time, time.monotonic
    time.time = lambda: 1_700_000_000.5  # type: ignore[assignment]
    time.monotonic = lambda: 500.0  # type: ignore[assignment]
    try:
        return [{"mono": mono, "wall": rest_gui._mono_to_wall(mono)} for mono in (500.0, 560.25, 0.0, 499.5)]
    finally:
        time.time, time.monotonic = real_time, real_monotonic  # type: ignore[assignment]


def main() -> None:
    corpus = {
        "mono_to_wall": {"wall_now": 1_700_000_000.5, "mono_now": 500.0, "samples": _mono_to_wall_samples()},
        "capability": rest_gui.CAP_ATTACH,
        "key_syms": dict(rest_gui._KEY_SYMS),
        "button_masks": dict(rest_gui._BUTTON_MASKS),
        "console": list(CONSOLE),
        "paths": {"attach": ATTACH, "screenshot": SHOT, "click": CLICK, "type": TYPE, "key": KEY, "drag": DRAG},
        "int_fields": [{"raw": raw, "value": rest_gui._int_field({"k": raw}, "k")} for raw in INT_FIELDS],
        "int_field_absent": rest_gui._int_field({}, "k"),
        "int_field_default": rest_gui._int_field({}, "k", 5),
        "int_field_default_used": rest_gui._int_field({"k": "no"}, "k", 5),
        "str_fields": [{"raw": raw, "value": rest_gui._str_field({"k": raw}, "k")} for raw in STR_FIELDS],
        "str_field_absent": rest_gui._str_field({}, "k"),
        "str_field_default": rest_gui._str_field({}, "k", "d"),
        "str_field_default_used": rest_gui._str_field({"k": 7}, "k", "d"),
        "cases": [{**case, **asyncio.run(_run(case))} for case in CASES],
    }
    OUT.write_text(json.dumps(corpus, indent=2) + "\n")
    print(f"wrote {OUT} ({len(corpus['cases'])} cases)")


if __name__ == "__main__":
    main()
