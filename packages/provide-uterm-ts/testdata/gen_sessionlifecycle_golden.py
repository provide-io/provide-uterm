#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the session runtime's lifecycle.

What a Durable Object does when a socket opens, closes or fails. The plumbing
is Cloudflare's; the decisions are not, and three of them matter:

* **What a browser is told on connect.** The hello frame carries
  ``can_hijack``, and it is the *JWT-resolved* role that decides it — not what
  the browser asked for. A port that read the requested role would show hijack
  controls to a viewer.
* **Whether hello is sent at all.** On a normal upgrade ``fetch()`` has
  already sent it before the 101; on a hibernation restore ``fetch()`` never
  ran. Getting that wrong means a browser gets two hellos or none.
* **What a disconnect leaves behind.** A browser that held the hijack has its
  resume token marked so it can reclaim ownership on reconnect; a worker
  leaving moves the session to ``stopped`` on a clean close and ``error`` on a
  failure. A session already deleted does none of it — there is nobody left to
  tell.

The real handlers are driven here, not transcribed: the mixin reads everything
it needs off ``self``, so a recording stub standing in for the Durable Object
is enough to make it run, and what is recorded is what it actually did.

# uv-package: provide-uterm-cloudflare

Usage (from the repository root)::

    uv run --package provide-uterm-cloudflare python \\
        packages/provide-uterm-ts/testdata/gen_sessionlifecycle_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from provide.uterm.cloudflare.do.session_runtime import lifecycle

OUT = Path(__file__).resolve().parent / "sessionlifecycle_golden.json"

FIXED_TS = 1_700_000_000.0
RESUME_TOKEN = "recorded-resume-token"  # noqa: S105 — a fixed stand-in, not a credential
WS_ID = "ws-1"
WORKER_ID = "w1"


class FakeSocket:
    """A socket that records only that it was closed."""

    def __init__(self) -> None:
        self.closed: list[tuple[int, str]] = []

    def close(self, code: int, reason: str) -> None:
        self.closed.append((code, reason))


class RecordingRuntime:
    """Everything ``_LifecycleMixin`` reads off ``self``, and nothing else.

    Every effect is appended to :attr:`actions` in the order it happened, so
    the corpus records the sequence and not merely the end state.
    """

    def __init__(self, case: dict[str, Any]) -> None:
        self.actions: list[dict[str, Any]] = []
        self._case = case
        self._role = case.get("role", "browser")
        self._deleted_at = 1.0 if case.get("deleted") else None
        self.worker_id = WORKER_ID
        self.worker_ws = None
        self.env = object()
        self.input_mode = case.get("input_mode", "open")
        self.meta = {"presence": case.get("presence", False)}
        self.lifecycle_state = "starting"
        self.browser_sockets = {WS_ID: object()} if case.get("already_initialized") else {}
        self.raw_sockets: dict[str, Any] = {}
        self.last_snapshot = {"type": "snapshot", "screen": "the screen"} if case.get("has_snapshot", True) else None
        self.browser_resume_tokens = {WS_ID: RESUME_TOKEN} if case.get("has_resume_token") else {}
        self.browser_hijack_owner = {WS_ID: "principal"} if case.get("held_hijack") else {}
        self.config = _Config(case.get("resume_on", True))
        self.store = _Store(self.actions)
        self.hijack = _Hijack()
        self._ushell = None

    # ── the accessors the mixin calls ────────────────────────────────────
    def ws_key(self, _ws: Any) -> str:
        return WS_ID

    def _socket_role(self, _ws: Any) -> str:
        return self._role

    def _socket_browser_role(self, _ws: Any) -> str:
        return self._case.get("browser_role", "viewer")

    def _socket_worker_id(self, _ws: Any) -> str:
        return WORKER_ID

    # ── the effects ──────────────────────────────────────────────────────
    def _register_socket(self, _ws: Any, role: str) -> None:
        self.actions.append({"kind": "register_socket", "role": role})

    def _remove_ws(self, _ws: Any) -> None:
        self.actions.append({"kind": "remove_socket"})

    async def broadcast_worker_frame(self, frame: dict[str, Any]) -> None:
        self.actions.append({"kind": "broadcast_worker_frame", "frame": _fix(frame)})

    async def broadcast_to_browsers(self, frame: dict[str, Any]) -> None:
        self.actions.append({"kind": "broadcast_browsers", "frame": _fix(frame)})

    async def send_ws(self, _ws: Any, frame: dict[str, Any]) -> None:
        self.actions.append({"kind": "send", "frame": _fix(frame)})

    async def _send_text(self, _ws: Any, text: str) -> None:
        self.actions.append({"kind": "send_text", "text": text})

    async def _maybe_send_presence_sync(self, _ws: Any, exclude_self: bool = False) -> None:
        self.actions.append({"kind": "presence_sync", "exclude_self": exclude_self})

    async def send_hijack_state(self, _ws: Any) -> None:
        self.actions.append({"kind": "send_hijack_state"})


class _Config:
    def __init__(self, resume_enabled: bool) -> None:
        self.resume_enabled = resume_enabled
        self.resume_ttl_s = 300.0


class _Hijack:
    session = None


class _Store:
    def __init__(self, actions: list[dict[str, Any]]) -> None:
        self._actions = actions

    def create_resume_token(self, token: str, worker_id: str, role: str, ttl: float) -> None:
        self._actions.append(
            {"kind": "create_resume_token", "token": token, "worker_id": worker_id, "role": role, "ttl": ttl}
        )

    def mark_resume_hijack_owner(self, token: str, owner: bool) -> None:
        self._actions.append({"kind": "mark_resume_hijack_owner", "token": token, "owner": owner})


def _fix(frame: dict[str, Any]) -> dict[str, Any]:
    """Replace the wall clock so the corpus does not differ from itself."""
    fixed = dict(frame)
    if "ts" in fixed:
        fixed["ts"] = FIXED_TS
    if isinstance(fixed.get("protocol"), dict):
        fixed["protocol"] = dict(fixed["protocol"])
    return fixed


async def _run(case: dict[str, Any], event: str) -> dict[str, Any]:
    """Drive one real handler and record everything it did."""
    runtime = RecordingRuntime(case)
    socket = FakeSocket()
    kv_calls: list[dict[str, Any]] = []

    async def fake_update_kv(_env: Any, worker_id: str, **kwargs: Any) -> None:
        kv_calls.append({"worker_id": worker_id, **{key: value for key, value in kwargs.items() if key != "meta"}})
        runtime.actions.append({"kind": "update_kv", "connected": kwargs.get("connected")})

    async def fake_on_browser_connected(_runtime: Any) -> None:
        runtime.actions.append({"kind": "on_browser_connected"})

    with (
        patch.object(lifecycle, "update_kv_session", fake_update_kv),
        patch.object(lifecycle, "on_browser_connected", fake_on_browser_connected),
        patch.object(lifecycle.secrets, "token_urlsafe", lambda _n: RESUME_TOKEN),
        patch.object(lifecycle.time, "time", lambda: FIXED_TS),
    ):
        if event == "open":
            await lifecycle._LifecycleMixin.webSocketOpen(runtime, socket)
        elif event == "close":
            await lifecycle._LifecycleMixin.webSocketClose(runtime, socket, 1000, "bye")
        else:
            await lifecycle._LifecycleMixin.webSocketError(runtime, socket, RuntimeError("boom"))

    return {
        "actions": runtime.actions,
        "closed": [{"code": code, "reason": reason} for code, reason in socket.closed],
        "lifecycle_state": runtime.lifecycle_state,
        "kv": kv_calls,
    }


OPEN_CASES: list[tuple[str, dict[str, Any]]] = [
    ("a worker arriving", {"role": "worker"}),
    ("a worker arriving at a deleted session", {"role": "worker", "deleted": True}),
    ("a raw socket arriving with a screen to show", {"role": "raw", "has_snapshot": True}),
    ("a raw socket arriving with nothing to show", {"role": "raw", "has_snapshot": False}),
    (
        "an admin browser on a fresh upgrade",
        {"role": "browser", "browser_role": "admin", "already_initialized": True, "presence": True},
    ),
    (
        "an admin browser after hibernation",
        {"role": "browser", "browser_role": "admin", "already_initialized": False, "presence": True},
    ),
    ("an operator browser after hibernation", {"role": "browser", "browser_role": "operator"}),
    ("a viewer browser after hibernation", {"role": "browser", "browser_role": "viewer"}),
    ("a browser with a role nobody defined", {"role": "browser", "browser_role": "superuser"}),
    ("a browser after hibernation with resume off", {"role": "browser", "browser_role": "admin", "resume_on": False}),
    (
        "a browser after hibernation with no screen yet",
        {"role": "browser", "browser_role": "admin", "has_snapshot": False},
    ),
    ("a browser arriving at a deleted session", {"role": "browser", "deleted": True}),
    ("a browser on a session in hijack mode", {"role": "browser", "browser_role": "admin", "input_mode": "hijack"}),
]

DISCONNECT_CASES: list[tuple[str, dict[str, Any]]] = [
    ("a browser leaving a presence session", {"role": "browser", "presence": True}),
    ("a browser leaving a session without presence", {"role": "browser", "presence": False}),
    (
        "a browser that held the hijack leaving",
        {"role": "browser", "held_hijack": True, "has_resume_token": True},
    ),
    (
        "a browser that held the hijack with no resume token",
        {"role": "browser", "held_hijack": True, "has_resume_token": False},
    ),
    ("a browser leaving a deleted session", {"role": "browser", "presence": True, "deleted": True}),
    ("a worker leaving", {"role": "worker"}),
    ("a worker leaving a deleted session", {"role": "worker", "deleted": True}),
    ("a raw socket leaving", {"role": "raw"}),
]


async def main_async() -> None:
    corpus = {
        "fixed_ts": FIXED_TS,
        "resume_token": RESUME_TOKEN,
        "ws_id": WS_ID,
        "worker_id": WORKER_ID,
        "open": [{"name": name, "case": case, **await _run(case, "open")} for name, case in OPEN_CASES],
        "close": [{"name": name, "case": case, **await _run(case, "close")} for name, case in DISCONNECT_CASES],
        "error": [{"name": name, "case": case, **await _run(case, "error")} for name, case in DISCONNECT_CASES],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['open'])} opens, {len(corpus['close'])} closes)")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
