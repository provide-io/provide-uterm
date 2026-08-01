#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the session runtime's I/O rules.

Three decisions, all of them recorded by driving the real code rather than
reading it.

* **What a request body has to look like.** ``application/json`` is required,
  and that is a CSRF guard rather than a formality: a browser can send
  ``text/plain`` or a form encoding cross-origin *without* a preflight, so a
  handler that parsed those would take instructions from any page a session
  owner happened to visit. A body over 64 KB is refused too — a Durable Object
  has little memory to lose.
* **Who is told they hold the hijack.** ``owner`` is ``me`` only when this
  browser's recorded hijack id matches the live session's; every other
  browser is told ``other``. Reading it any other way would show one viewer
  another's controls.
* **How a lease expiry crosses a restart.** It is monotonic in memory and
  wall-clock on disk, and the two conversions are what keeps a countdown
  meaning the same thing after a Durable Object restarts.

# uv-package: provide-uterm-cloudflare

Usage (from the repository root)::

    uv run --package provide-uterm-cloudflare python \\
        packages/provide-uterm-ts/testdata/gen_sessionio_golden.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from provide.uterm.cloudflare.do.session_runtime import io as session_io

OUT = Path(__file__).resolve().parent / "sessionio_golden.json"

FIXED_TS = 1_700_000_000.0
FIXED_MONO = 500.0
WS_ID = "ws-1"


class FakeHeaders:
    def __init__(self, headers: dict[str, str]) -> None:
        self._headers = headers

    def get(self, name: str) -> str | None:
        return self._headers.get(name)


class FakeRequest:
    def __init__(self, headers: dict[str, str], body: str) -> None:
        self.headers = FakeHeaders(headers)
        self._body = body

    async def text(self) -> str:
        return self._body


class RecordingRuntime:
    """Only what the I/O mixin reads off ``self``."""

    def __init__(self, case: dict[str, Any] | None = None) -> None:
        case = case or {}
        self.sent: list[dict[str, Any]] = []
        self.dropped: list[str] = []
        self.input_mode = case.get("input_mode", "open")
        self.hijack = _Hijack(case.get("hijack_id"), case.get("lease_expires_at"))
        self.browser_hijack_owner = dict(case.get("owners", {}))
        self.browser_sockets = {ws_id: _Socket(ws_id) for ws_id in case.get("sockets", [])}
        self._failing = set(case.get("failing_sockets", []))
        # The full remove_browser_socket path a failed broadcast now takes.
        self.browser_resume_tokens: dict[str, str] = {}
        self.store = SimpleNamespace(mark_resume_hijack_owner=lambda _token, _flag: None)

    def ws_key(self, ws: Any) -> str:
        return ws.ws_id

    def input_delivery_guard(self) -> Any:
        return contextlib.nullcontext()

    def _restore_browser_identity(self, ws: Any) -> None:
        pass

    def _set_browser_ownership_attachment(self, ws: Any, hijack_id: str | None) -> None:
        if hijack_id is None:
            self.browser_hijack_owner.pop(ws.ws_id, None)
        else:
            self.browser_hijack_owner[ws.ws_id] = hijack_id

    def _remove_ws(self, ws: Any) -> None:
        self.browser_sockets.pop(ws.ws_id, None)
        self.dropped.append(ws.ws_id)

    def clear_lease(self) -> None:
        self.hijack.session = None

    async def push_worker_control(self, op: str, **_kwargs: Any) -> None:
        pass

    async def send_ws(self, ws: Any, frame: dict[str, Any]) -> None:
        if ws.ws_id in self._failing:
            raise RuntimeError("socket gone")
        self.sent.append({"ws_id": ws.ws_id, "frame": _fix(frame)})

    async def send_hijack_state(self, ws: Any) -> None:
        # The real one, so a broadcast exercises the frame builder rather than
        # a stand-in for it.
        await session_io._SessionRuntimeIoMixin.send_hijack_state(self, ws)

    async def remove_browser_socket(self, ws: Any) -> bool:
        # The real one, so a dead socket's ownership release is what the
        # reference actually does rather than a stand-in for it.
        return await session_io._SessionRuntimeIoMixin.remove_browser_socket(self, ws)


class _Socket:
    def __init__(self, ws_id: str) -> None:
        self.ws_id = ws_id


class _Session:
    def __init__(self, hijack_id: str, lease_expires_at: float | None) -> None:
        self.hijack_id = hijack_id
        self.lease_expires_at = lease_expires_at


class _Hijack:
    def __init__(self, hijack_id: str | None, lease_expires_at: float | None) -> None:
        self.session = None if hijack_id is None else _Session(hijack_id, lease_expires_at)

    def release(self, hijack_id: str) -> Any:
        ok = self.session is not None and self.session.hijack_id == hijack_id
        if ok:
            self.session = None
        return SimpleNamespace(ok=ok)


def _fix(frame: dict[str, Any]) -> dict[str, Any]:
    fixed = dict(frame)
    if "ts" in fixed:
        fixed["ts"] = FIXED_TS
    return fixed


REQUESTS: list[tuple[str, dict[str, str], str]] = [
    ("an ordinary JSON body", {"Content-Type": "application/json"}, '{"a":1}'),
    ("a charset alongside the type", {"Content-Type": "application/json; charset=utf-8"}, '{"a":1}'),
    ("the type in capitals", {"Content-Type": "APPLICATION/JSON"}, '{"a":1}'),
    ("a type with spaces around it", {"Content-Type": "  application/json  "}, '{"a":1}'),
    ("plain text, which needs no preflight", {"Content-Type": "text/plain"}, '{"a":1}'),
    ("a form encoding, which needs no preflight", {"Content-Type": "application/x-www-form-urlencoded"}, '{"a":1}'),
    ("multipart, which needs no preflight", {"Content-Type": "multipart/form-data"}, '{"a":1}'),
    ("no content type at all", {}, '{"a":1}'),
    ("an empty content type", {"Content-Type": ""}, '{"a":1}'),
    ("a type that merely contains the words", {"Content-Type": "text/plain+application/json"}, '{"a":1}'),
    ("an empty body", {"Content-Type": "application/json"}, ""),
    ("a body that is not JSON", {"Content-Type": "application/json"}, "not json"),
    ("a JSON list", {"Content-Type": "application/json"}, "[1,2]"),
    ("a JSON string", {"Content-Type": "application/json"}, '"hello"'),
    ("a JSON null", {"Content-Type": "application/json"}, "null"),
    ("a JSON number", {"Content-Type": "application/json"}, "42"),
    ("a nested object", {"Content-Type": "application/json"}, '{"a":{"b":[1,2]}}'),
    ("a body at the cap", {"Content-Type": "application/json"}, '{"a":"' + "x" * (65_536 - 8) + '"}'),
    ("a body over the cap", {"Content-Type": "application/json"}, '{"a":"' + "x" * (65_536 - 7) + '"}'),
]

HIJACK_STATES: list[tuple[str, dict[str, Any]]] = [
    ("nobody is hijacking", {"sockets": ["ws-1"]}),
    (
        "the browser that is hijacking",
        {"sockets": ["ws-1"], "hijack_id": "h1", "owners": {"ws-1": "h1"}, "lease_expires_at": 560.0},
    ),
    (
        "a browser that is not",
        {"sockets": ["ws-1"], "hijack_id": "h1", "owners": {"ws-1": "h2"}, "lease_expires_at": 560.0},
    ),
    (
        "a browser with no recorded owner",
        {"sockets": ["ws-1"], "hijack_id": "h1", "owners": {}, "lease_expires_at": 560.0},
    ),
    (
        "a hijack with no expiry",
        {"sockets": ["ws-1"], "hijack_id": "h1", "owners": {"ws-1": "h1"}, "lease_expires_at": None},
    ),
    ("a session in hijack input mode", {"sockets": ["ws-1"], "input_mode": "hijack"}),
]

BROADCASTS: list[tuple[str, dict[str, Any]]] = [
    ("nobody watching", {"sockets": []}),
    ("one browser", {"sockets": ["ws-1"]}),
    (
        "three browsers, one of them holding the hijack",
        {"sockets": ["ws-1", "ws-2", "ws-3"], "hijack_id": "h1", "owners": {"ws-2": "h1"}, "lease_expires_at": 560.0},
    ),
    (
        "a browser whose socket has gone",
        {"sockets": ["ws-1", "ws-2"], "failing_sockets": ["ws-1"]},
    ),
    (
        "a browser that has gone and held the hijack",
        {
            "sockets": ["ws-1", "ws-2"],
            "failing_sockets": ["ws-1"],
            "hijack_id": "h1",
            "owners": {"ws-1": "h1"},
            "lease_expires_at": 560.0,
        },
    ),
]


async def _request(headers: dict[str, str], body: str) -> dict[str, Any] | str:
    runtime = RecordingRuntime()
    try:
        return await session_io._SessionRuntimeIoMixin.request_json(runtime, FakeRequest(headers, body))
    except Exception as exc:
        return {"raises": type(exc).__name__}


async def _hijack_state(case: dict[str, Any]) -> dict[str, Any]:
    runtime = RecordingRuntime(case)
    socket = next(iter(runtime.browser_sockets.values()))
    with (
        patch.object(session_io.time, "time", lambda: FIXED_TS),
        patch.object(session_io.time, "monotonic", lambda: FIXED_MONO),
    ):
        await session_io._SessionRuntimeIoMixin.send_hijack_state(runtime, socket)
    return {"sent": runtime.sent}


async def _broadcast(case: dict[str, Any]) -> dict[str, Any]:
    runtime = RecordingRuntime(case)
    with (
        patch.object(session_io.time, "time", lambda: FIXED_TS),
        patch.object(session_io.time, "monotonic", lambda: FIXED_MONO),
    ):
        await session_io._SessionRuntimeIoMixin.broadcast_hijack_state(runtime)
    return {
        "sent": runtime.sent,
        "sockets_left": sorted(runtime.browser_sockets),
        "owners_left": dict(sorted(runtime.browser_hijack_owner.items())),
    }


async def main_async() -> None:
    with (
        patch.object(session_io.time, "time", lambda: FIXED_TS),
        patch.object(session_io.time, "monotonic", lambda: FIXED_MONO),
    ):
        conversions = [
            {"mono": mono, "wall": session_io._mono_to_wall(mono)} for mono in (None, 0.0, 500.0, 560.0, -1.0)
        ]
        back = [{"wall": wall, "mono": session_io._wall_to_mono(wall)} for wall in (FIXED_TS, FIXED_TS + 60.0, 0.0)]

    corpus = {
        "fixed_ts": FIXED_TS,
        "fixed_mono": FIXED_MONO,
        "max_request_body": session_io._MAX_REQUEST_BODY,
        "max_inflight_webhooks": session_io._MAX_INFLIGHT_WEBHOOKS,
        "requests": [
            {
                "name": name,
                "content_type": headers.get("Content-Type"),
                "body_len": len(body),
                "parsed": await _request(headers, body),
            }
            for name, headers, body in REQUESTS
        ],
        "hijack_state": [{"name": name, "case": case, **await _hijack_state(case)} for name, case in HIJACK_STATES],
        "broadcast": [{"name": name, "case": case, **await _broadcast(case)} for name, case in BROADCASTS],
        "mono_to_wall": conversions,
        "wall_to_mono": back,
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['requests'])} requests)")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
