#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the session, fan-out and GUI tools.

The nineteen tools that finish the MCP surface: twelve that manage sessions,
watch them, broadcast to a group of them and annotate them, and seven that
reach a graphical console. Most are the same shape as the hijack tools, and
what is worth recording is where they are not:

* **``session_create`` is the widest tool there is** — it can spawn a
  connector — so its whole configuration is vetted before any call.
* **``session_watch`` and ``session_subscribe`` are clamped.** A model asking
  for an hour of events and a million of them gets thirty seconds and fifty,
  or two minutes and five hundred. Neither ceiling is a suggestion.
* **``session_subscribe`` re-checks the pattern itself** against each event's
  screen, rather than trusting that events arriving means the pattern fired:
  the fallback path, taken when there is no event bus, does not filter.
* **A path lands in a URL** for the fan-out and annotation tools, which is why
  the id is checked first — and why what is recorded is that nothing was
  called at all when it is bad.

Driven for real: the tools are registered on an MCP instance and invoked
through the same authorization decorator that guards them, against a client
that records what it was asked.

# uv-package: provide-uterm-client

Usage (from the repository root)::

    uv run --package provide-uterm-client python \\
        packages/provide-uterm-ts/testdata/gen_mcpsessiontools_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from provide.uterm.ai.auth import AuthorizationContext, McpPrincipal
from provide.uterm.ai.server_tools_gui import register_gui_tools
from provide.uterm.ai.server_tools_session import register_session_tools

OUT = Path(__file__).resolve().parent / "mcpsessiontools_golden.json"

SCREEN = "\x1b[31mred\x1b[0m\nplain\nlast"
SNAPSHOT = {"screen": SCREEN, "cursor": [1, 2], "cols": 80, "rows": 24, "extra": "kept"}

SESSION_TOOLS = [
    "session_list",
    "session_status",
    "session_read",
    "session_connect",
    "session_disconnect",
    "session_create",
    "session_watch",
    "session_subscribe",
    "fanout_group_create",
    "fanout_send",
    "session_annotate",
]

GUI_TOOLS = [
    "gui_hijack_begin",
    "gui_hijack_release",
    "gui_screenshot",
    "gui_click",
    "gui_type",
    "gui_key",
    "gui_drag",
]

# Events as the server sends them, so the pattern re-check has something real
# to run against.
EVENTS = {
    "events": [
        {"type": "snapshot", "data": {"screen": "waiting"}},
        {"type": "input_send", "data": {"keys": "ls"}},
        {"type": "snapshot", "data": {"screen": "ada@host:~$ "}},
    ]
}


class _Client:
    """A client that records what it was asked and answers to script."""

    def __init__(self, ok: bool = True, data: Any = None) -> None:
        self.ok = ok
        self.data = data
        self.calls: list[dict[str, Any]] = []

    async def _record(self, method: str, **kwargs: Any) -> tuple[bool, Any]:
        self.calls.append({"method": method, **kwargs})
        return self.ok, self.data

    async def list_sessions(self) -> tuple[bool, Any]:
        return await self._record("list_sessions")

    async def get_session(self, session_id: str) -> tuple[bool, Any]:
        return await self._record("get_session", session_id=session_id)

    async def session_snapshot(self, session_id: str) -> tuple[bool, Any]:
        return await self._record("session_snapshot", session_id=session_id)

    async def connect_session(self, session_id: str) -> tuple[bool, Any]:
        return await self._record("connect_session", session_id=session_id)

    async def disconnect_session(self, session_id: str) -> tuple[bool, Any]:
        return await self._record("disconnect_session", session_id=session_id)

    async def quick_connect(self, connector_type: str, **kwargs: Any) -> tuple[bool, Any]:
        return await self._record("quick_connect", connector_type=connector_type, **kwargs)

    async def watch_session_events(self, session_id: str, **kwargs: Any) -> tuple[bool, Any]:
        return await self._record("watch_session_events", session_id=session_id, **kwargs)

    async def post(self, path: str, json: Any = None) -> tuple[bool, Any]:
        return await self._record("post", path=path, body=json)

    async def acquire(self, worker_id: str, **kwargs: Any) -> tuple[bool, Any]:
        return await self._record("acquire", worker_id=worker_id, **kwargs)

    async def release(self, worker_id: str, hijack_id: str) -> tuple[bool, Any]:
        return await self._record("release", worker_id=worker_id, hijack_id=hijack_id)

    async def gui_screenshot(self, worker_id: str, hijack_id: str) -> tuple[bool, Any]:
        return await self._record("gui_screenshot", worker_id=worker_id, hijack_id=hijack_id)

    async def gui_click(self, worker_id: str, hijack_id: str, **kwargs: Any) -> tuple[bool, Any]:
        return await self._record("gui_click", worker_id=worker_id, hijack_id=hijack_id, **kwargs)

    async def gui_type(self, worker_id: str, hijack_id: str, **kwargs: Any) -> tuple[bool, Any]:
        return await self._record("gui_type", worker_id=worker_id, hijack_id=hijack_id, **kwargs)

    async def gui_key(self, worker_id: str, hijack_id: str, **kwargs: Any) -> tuple[bool, Any]:
        return await self._record("gui_key", worker_id=worker_id, hijack_id=hijack_id, **kwargs)

    async def gui_drag(self, worker_id: str, hijack_id: str, **kwargs: Any) -> tuple[bool, Any]:
        return await self._record("gui_drag", worker_id=worker_id, hijack_id=hijack_id, **kwargs)


# (name, tool, args, ok, data[, "diverges"]) — the last field marks a case the
# port answers differently on purpose.
CASES: list[tuple[Any, ...]] = [
    # --- managing sessions -------------------------------------------------
    ("listing sessions", "session_list", {}, True, {"sessions": []}),
    ("listing when the server is unhappy", "session_list", {}, False, {"error": "down"}),
    ("asking after a session", "session_status", {"session_id": "s-1"}, True, {"status": "running"}),
    ("asking after a session that is a path", "session_status", {"session_id": "a/b"}, True, {}),
    ("reading a session", "session_read", {"session_id": "s-1"}, True, {"snapshot": SNAPSHOT}),
    (
        "reading a session as it stands",
        "session_read",
        {"session_id": "s-1", "output": "raw"},
        True,
        {"snapshot": SNAPSHOT},
    ),
    (
        "reading a session laid out",
        "session_read",
        {"session_id": "s-1", "output": "rendered"},
        True,
        {"snapshot": SNAPSHOT},
    ),
    (
        "reading the last line of a session",
        "session_read",
        {"session_id": "s-1", "tail_lines": 1},
        True,
        {"snapshot": SNAPSHOT},
    ),
    ("reading a session with nothing on screen", "session_read", {"session_id": "s-1"}, True, {"snapshot": {}}),
    (
        "reading a session whose snapshot is only a screen",
        "session_read",
        {"session_id": "s-1"},
        True,
        {"snapshot": {"screen": SCREEN}},
    ),
    ("reading a session that said nothing", "session_read", {"session_id": "s-1"}, True, {"other": 1}),
    ("reading a session that failed", "session_read", {"session_id": "s-1"}, False, {"snapshot": SNAPSHOT}),
    ("reading a session that is a path", "session_read", {"session_id": "a/b"}, True, {}),
    ("connecting a session", "session_connect", {"session_id": "s-1"}, True, {"connected": True}),
    ("connecting a session that is a path", "session_connect", {"session_id": "../etc"}, True, {}),
    ("disconnecting a session", "session_disconnect", {"session_id": "s-1"}, True, {"ok": True}),
    ("disconnecting a session that is a path", "session_disconnect", {"session_id": "a/b"}, True, {}),
    # --- creating one ------------------------------------------------------
    (
        "creating a local session",
        "session_create",
        {"connector_type": "local"},
        True,
        {"session_id": "s-9"},
    ),
    (
        "creating one with everything filled in",
        "session_create",
        {
            "connector_type": "ssh",
            "display_name": "box",
            "host": "example.test",
            "port": 22,
            "username": "ada",
            "password": "hunter2",
            "input_mode": "open",
        },
        True,
        {},
    ),
    (
        "creating one with fields nobody filled in",
        "session_create",
        {"connector_type": "ssh", "host": "example.test", "display_name": None, "username": None},
        True,
        {},
    ),
    (
        "creating one on a connector nobody allows",
        "session_create",
        {"connector_type": "exec"},
        True,
        {},
    ),
    (
        "creating one pointed at a port that is not one",
        "session_create",
        {"connector_type": "telnet", "host": "example.test", "port": 0},
        True,
        {},
    ),
    (
        "creating one pointed inside the network",
        "session_create",
        {"connector_type": "telnet", "host": "127.0.0.1", "port": 23},
        True,
        {},
    ),
    (
        "creating one pointed at a scheme nobody allows",
        "session_create",
        {"connector_type": "websocket", "url": "file:///etc/passwd"},
        True,
        {},
    ),
    (
        "creating one with a url pointed inside the network",
        "session_create",
        {"connector_type": "websocket", "url": "ws://localhost:8080/x"},
        True,
        {},
    ),
    # --- watching ----------------------------------------------------------
    ("watching a session", "session_watch", {"session_id": "s-1"}, True, EVENTS),
    (
        "watching for particular events",
        "session_watch",
        {"session_id": "s-1", "event_types": "snapshot,input_send"},
        True,
        EVENTS,
    ),
    ("watching for a pattern", "session_watch", {"session_id": "s-1", "pattern": "\\$ $"}, True, EVENTS),
    (
        "watching for a pattern that could hang",
        "session_watch",
        {"session_id": "s-1", "pattern": "(a+)+$"},
        True,
        EVENTS,
    ),
    ("watching for longer than allowed", "session_watch", {"session_id": "s-1", "timeout_s": 600}, True, EVENTS),
    ("watching for less than a moment", "session_watch", {"session_id": "s-1", "timeout_s": 0}, True, EVENTS),
    ("watching for a negative time", "session_watch", {"session_id": "s-1", "timeout_s": -5}, True, EVENTS),
    ("watching for a fraction of a second", "session_watch", {"session_id": "s-1", "timeout_s": 0.25}, True, EVENTS),
    ("watching for more events than allowed", "session_watch", {"session_id": "s-1", "max_events": 5000}, True, EVENTS),
    ("watching for no events at all", "session_watch", {"session_id": "s-1", "max_events": 0}, True, EVENTS),
    (
        "watching for a negative number of events",
        "session_watch",
        {"session_id": "s-1", "max_events": -3},
        True,
        EVENTS,
    ),
    ("watching a session that is a path", "session_watch", {"session_id": "a/b"}, True, EVENTS),
    # --- subscribing --------------------------------------------------------
    ("subscribing to a session", "session_subscribe", {"session_id": "s-1"}, True, EVENTS),
    (
        "subscribing with a pattern that fires",
        "session_subscribe",
        {"session_id": "s-1", "pattern": "\\$ $"},
        True,
        EVENTS,
    ),
    (
        "subscribing with a pattern that does not",
        "session_subscribe",
        {"session_id": "s-1", "pattern": "never-appears"},
        True,
        EVENTS,
    ),
    (
        "subscribing with a pattern the server refused for",
        "session_subscribe",
        {"session_id": "s-1", "pattern": "\\$ $"},
        False,
        EVENTS,
    ),
    (
        "subscribing with a pattern that could hang",
        "session_subscribe",
        {"session_id": "s-1", "pattern": "(a+)+$"},
        True,
        EVENTS,
    ),
    (
        "subscribing where an event is not a mapping",
        "session_subscribe",
        {"session_id": "s-1", "pattern": "prompt"},
        True,
        {"events": ["not a mapping", {"data": {"screen": "prompt here"}}]},
    ),
    (
        "subscribing where an event carries nothing",
        "session_subscribe",
        {"session_id": "s-1", "pattern": "x"},
        True,
        {"events": [{"type": "snapshot"}, {"type": "snapshot", "data": None}]},
    ),
    (
        "subscribing where the payload is not a mapping",
        "session_subscribe",
        {"session_id": "s-1", "pattern": "x"},
        True,
        {"events": [{"data": "a string"}]},
    ),
    (
        "subscribing where an event is nothing at all",
        "session_subscribe",
        {"session_id": "s-1", "pattern": "found"},
        True,
        {"events": [None, {"data": {"screen": "found it"}}]},
    ),
    # Recorded, and deliberately not reproduced: the reference renders a null
    # screen as the four characters ``None`` and matches a pattern against
    # them, so ``^N`` fires on a screen the terminal never showed. See the
    # port's own test for what it does instead.
    (
        "subscribing where the screen is nothing at all",
        "session_subscribe",
        {"session_id": "s-1", "pattern": "None"},
        True,
        {"events": [{"data": {"screen": None}}]},
        "diverges",
    ),
    (
        "subscribing where the screen is missing entirely",
        "session_subscribe",
        {"session_id": "s-1", "pattern": "None"},
        True,
        {"events": [{"data": {}}]},
    ),
    (
        "subscribing where the screen is not text",
        "session_subscribe",
        {"session_id": "s-1", "pattern": "42"},
        True,
        {"events": [{"data": {"screen": 42}}]},
    ),
    (
        "subscribing where there are no events at all",
        "session_subscribe",
        {"session_id": "s-1", "pattern": "x"},
        True,
        {},
    ),
    (
        "subscribing for longer than allowed",
        "session_subscribe",
        {"session_id": "s-1", "duration_s": 9999},
        True,
        EVENTS,
    ),
    ("subscribing for less than a second", "session_subscribe", {"session_id": "s-1", "duration_s": 0.1}, True, EVENTS),
    (
        "subscribing for more events than allowed",
        "session_subscribe",
        {"session_id": "s-1", "max_events": 100000},
        True,
        EVENTS,
    ),
    ("subscribing for no events at all", "session_subscribe", {"session_id": "s-1", "max_events": 0}, True, EVENTS),
    ("subscribing to a session that is a path", "session_subscribe", {"session_id": "a/b"}, True, EVENTS),
    # --- fanning out ---------------------------------------------------------
    (
        "making a group",
        "fanout_group_create",
        {"session_ids": ["s-1", "s-2"]},
        True,
        {"group_id": "g-1"},
    ),
    (
        "making a group on somebody's terms",
        "fanout_group_create",
        {"session_ids": ["s-1"], "name": "prod", "mode": "serial"},
        True,
        {},
    ),
    ("making a group of nothing", "fanout_group_create", {"session_ids": []}, True, {}),
    ("broadcasting to a group", "fanout_send", {"group_id": "g-1", "data": "ls\n"}, True, {"results": []}),
    (
        "broadcasting on somebody's terms",
        "fanout_send",
        {"group_id": "g-1", "data": "x", "quiesce_ms": 10, "max_response_ms": 20},
        True,
        {},
    ),
    ("broadcasting to a group that is a path", "fanout_send", {"group_id": "a/b", "data": "x"}, True, {}),
    # --- annotating -----------------------------------------------------------
    ("marking a moment", "session_annotate", {"session_id": "s-1", "label": "deploy"}, True, {"id": "a-1"}),
    (
        "marking a moment in full",
        "session_annotate",
        {"session_id": "s-1", "label": "oops", "description": "it broke", "severity": "error"},
        True,
        {},
    ),
    ("marking a session that is a path", "session_annotate", {"session_id": "a/b", "label": "x"}, True, {}),
    # --- the graphical console -------------------------------------------------
    ("taking a graphical lease", "gui_hijack_begin", {"worker_id": "w-1"}, True, {"hijack_id": "h-1"}),
    (
        "taking a graphical lease on somebody's terms",
        "gui_hijack_begin",
        {"worker_id": "w-1", "lease_s": 5, "owner": "ada"},
        True,
        {},
    ),
    ("taking a graphical lease on a path", "gui_hijack_begin", {"worker_id": "a/b"}, True, {}),
    ("giving a graphical lease back", "gui_hijack_release", {"worker_id": "w-1", "hijack_id": "h-1"}, True, {}),
    ("giving back a lease with a bad id", "gui_hijack_release", {"worker_id": "w-1", "hijack_id": "c/d"}, True, {}),
    ("taking a screenshot", "gui_screenshot", {"worker_id": "w-1", "hijack_id": "h-1"}, True, {"screenshot": "iVBOR"}),
    ("taking a screenshot with a bad id", "gui_screenshot", {"worker_id": "a/b", "hijack_id": "h-1"}, True, {}),
    ("clicking", "gui_click", {"worker_id": "w-1", "hijack_id": "h-1", "x": 3, "y": 4}, True, {"ok": True}),
    (
        "clicking another button",
        "gui_click",
        {"worker_id": "w-1", "hijack_id": "h-1", "x": 0, "y": 0, "button": "right"},
        True,
        {},
    ),
    ("clicking with a bad id", "gui_click", {"worker_id": "w-1", "hijack_id": "c/d", "x": 1, "y": 1}, True, {}),
    ("typing at a console", "gui_type", {"worker_id": "w-1", "hijack_id": "h-1", "text": "hello"}, True, {}),
    ("typing nothing at a console", "gui_type", {"worker_id": "w-1", "hijack_id": "h-1", "text": ""}, True, {}),
    ("typing at a console with a bad id", "gui_type", {"worker_id": "a/b", "hijack_id": "h-1", "text": "x"}, True, {}),
    ("pressing a key", "gui_key", {"worker_id": "w-1", "hijack_id": "h-1", "key_name": "Enter"}, True, {}),
    ("pressing a key with a bad id", "gui_key", {"worker_id": "a/b", "hijack_id": "h-1", "key_name": "Tab"}, True, {}),
    (
        "dragging",
        "gui_drag",
        {"worker_id": "w-1", "hijack_id": "h-1", "start_x": 1, "start_y": 2, "end_x": 3, "end_y": 4},
        True,
        {},
    ),
    (
        "dragging with a bad id",
        "gui_drag",
        {"worker_id": "a/b", "hijack_id": "h-1", "start_x": 0, "start_y": 0, "end_x": 1, "end_y": 1},
        True,
        {},
    ),
]


async def _run(tool_name: str, args: dict[str, Any], ok: bool, data: Any) -> dict[str, Any]:
    client = _Client(ok=ok, data=data)
    mcp: Any = FastMCP("differential")
    auth = AuthorizationContext(McpPrincipal(subject_id="u1", roles=frozenset({"admin"})))
    register_session_tools(mcp, client, auth)
    register_gui_tools(mcp, client, auth)
    tool = await mcp.get_tool(tool_name)
    return {"result": await tool.fn(**args), "calls": client.calls}


def main() -> None:
    corpus = {
        "screen": SCREEN,
        "snapshot": SNAPSHOT,
        "events": EVENTS,
        "session_tools": SESSION_TOOLS,
        "gui_tools": GUI_TOOLS,
        "cases": [
            {
                "name": case[0],
                "tool": case[1],
                "args": case[2],
                "ok": case[3],
                "data": case[4],
                "diverges": len(case) > 5,
                **asyncio.run(_run(case[1], case[2], case[3], case[4])),
            }
            for case in CASES
        ],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['cases'])} cases)")


if __name__ == "__main__":
    main()
