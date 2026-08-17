#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the MCP hijack + control tools.

Ten tools an LLM can call: six that drive a hijack lease, and four that ask
the server or a worker to change state. Each body is short, and all of the
substance is in the order of what it does:

* **Validate before you reach.** Every id is checked before the client is
  touched, so a caller-supplied path segment never reaches a request path.
  Where a tool takes two ids, whichever is bad first is the one reported.
* **A pattern is checked as an id is.** ``hijack_send`` takes a regex the
  caller wrote and refuses it here rather than forwarding it to a server that
  would also have to compile it.
* **Keystrokes are capped and sanitised** on the way through: what a model
  sends is not what reaches a terminal until it has been through that.
* **One answer shape.** Whatever the client returns is folded into a single
  dict carrying ``success``; a body that is not a mapping goes under ``data``
  rather than being spread, so a list can never invent fields.

The tools are registered on a real MCP instance and invoked through the same
authorization decorator that guards them in production, against a client that
records what it was asked to do.

# uv-package: provide-uterm-client

Usage (from the repository root)::

    uv run --package provide-uterm-client python \\
        packages/provide-uterm-ts/testdata/gen_mcphijacktools_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from provide.uterm.ai.auth import AuthorizationContext, McpPrincipal
from provide.uterm.ai.constants import MAX_KEYSTROKE_BYTES
from provide.uterm.ai.server_tools_hijack import register_hijack_tools

OUT = Path(__file__).resolve().parent / "mcphijacktools_golden.json"

# A snapshot as the server sends one, escapes and all, so the shaping is
# exercised for real rather than against a bare string.
SCREEN = "\x1b[31mred\x1b[0m\nplain\nlast"
SNAPSHOT = {"screen": SCREEN, "cursor": [1, 2], "cols": 80, "rows": 24, "extra": "kept"}

TOOLS = [
    "hijack_begin",
    "hijack_heartbeat",
    "hijack_read",
    "hijack_send",
    "hijack_step",
    "hijack_release",
    "server_health",
    "session_set_mode",
    "worker_input_mode",
    "worker_disconnect",
]


class _Client:
    """A hijack client that records what it was asked and answers to script."""

    def __init__(self, ok: bool = True, data: Any = None) -> None:
        self.ok = ok
        # Kept exactly as scripted, `None` included: a body that is not a
        # mapping is the case the answer-folding exists for.
        self.data = data
        self.calls: list[dict[str, Any]] = []

    async def _record(self, method: str, **kwargs: Any) -> tuple[bool, Any]:
        self.calls.append({"method": method, **kwargs})
        return self.ok, self.data

    async def acquire(self, worker_id: str, **kwargs: Any) -> tuple[bool, Any]:
        return await self._record("acquire", worker_id=worker_id, **kwargs)

    async def heartbeat(self, worker_id: str, hijack_id: str, **kwargs: Any) -> tuple[bool, Any]:
        return await self._record("heartbeat", worker_id=worker_id, hijack_id=hijack_id, **kwargs)

    async def snapshot(self, worker_id: str, hijack_id: str, **kwargs: Any) -> tuple[bool, Any]:
        return await self._record("snapshot", worker_id=worker_id, hijack_id=hijack_id, **kwargs)

    async def events(self, worker_id: str, hijack_id: str, **kwargs: Any) -> tuple[bool, Any]:
        return await self._record("events", worker_id=worker_id, hijack_id=hijack_id, **kwargs)

    async def send(self, worker_id: str, hijack_id: str, **kwargs: Any) -> tuple[bool, Any]:
        return await self._record("send", worker_id=worker_id, hijack_id=hijack_id, **kwargs)

    async def step(self, worker_id: str, hijack_id: str) -> tuple[bool, Any]:
        return await self._record("step", worker_id=worker_id, hijack_id=hijack_id)

    async def release(self, worker_id: str, hijack_id: str) -> tuple[bool, Any]:
        return await self._record("release", worker_id=worker_id, hijack_id=hijack_id)

    async def health(self) -> tuple[bool, Any]:
        return await self._record("health")

    async def set_session_mode(self, session_id: str, mode: str) -> tuple[bool, Any]:
        return await self._record("set_session_mode", session_id=session_id, mode=mode)

    async def set_input_mode(self, worker_id: str, mode: str) -> tuple[bool, Any]:
        return await self._record("set_input_mode", worker_id=worker_id, mode=mode)

    async def disconnect_worker(self, worker_id: str) -> tuple[bool, Any]:
        return await self._record("disconnect_worker", worker_id=worker_id)


BIG_KEYS = "x" * (MAX_KEYSTROKE_BYTES + 100)
LONG_PATTERN = "a" * 900

# (name, tool, args, ok, data)
CASES: list[tuple[str, str, dict[str, Any], bool, Any]] = [
    # --- beginning and keeping a lease -----------------------------------
    ("beginning a lease", "hijack_begin", {"worker_id": "w-1"}, True, {"hijack_id": "h-1"}),
    (
        "beginning a lease on somebody's terms",
        "hijack_begin",
        {"worker_id": "w-1", "lease_s": 5, "owner": "ada"},
        True,
        {},
    ),
    ("beginning a lease on a name that is a path", "hijack_begin", {"worker_id": "a/b"}, True, {}),
    ("beginning a lease the server refuses", "hijack_begin", {"worker_id": "w-1"}, False, {"error": "busy"}),
    ("keeping a lease", "hijack_heartbeat", {"worker_id": "w-1", "hijack_id": "h-1"}, True, {"lease_s": 90}),
    (
        "keeping a lease for longer",
        "hijack_heartbeat",
        {"worker_id": "w-1", "hijack_id": "h-1", "lease_s": 5},
        True,
        {},
    ),
    ("keeping a lease on a bad worker", "hijack_heartbeat", {"worker_id": "a/b", "hijack_id": "h-1"}, True, {}),
    ("keeping a lease with a bad hijack id", "hijack_heartbeat", {"worker_id": "w-1", "hijack_id": "c/d"}, True, {}),
    ("keeping a lease where both are bad", "hijack_heartbeat", {"worker_id": "a/b", "hijack_id": "c/d"}, True, {}),
    # --- reading -----------------------------------------------------------
    ("reading a snapshot", "hijack_read", {"worker_id": "w-1", "hijack_id": "h-1"}, True, {"snapshot": SNAPSHOT}),
    (
        "reading a snapshot as it stands",
        "hijack_read",
        {"worker_id": "w-1", "hijack_id": "h-1", "output": "raw"},
        True,
        {"snapshot": SNAPSHOT},
    ),
    (
        "reading a snapshot laid out",
        "hijack_read",
        {"worker_id": "w-1", "hijack_id": "h-1", "output": "rendered"},
        True,
        {"snapshot": SNAPSHOT},
    ),
    (
        "reading the last line only",
        "hijack_read",
        {"worker_id": "w-1", "hijack_id": "h-1", "tail_lines": 1},
        True,
        {"snapshot": SNAPSHOT},
    ),
    (
        "reading more lines than there are",
        "hijack_read",
        {"worker_id": "w-1", "hijack_id": "h-1", "tail_lines": 99},
        True,
        {"snapshot": SNAPSHOT},
    ),
    (
        "reading no lines at all",
        "hijack_read",
        {"worker_id": "w-1", "hijack_id": "h-1", "tail_lines": 0},
        True,
        {"snapshot": SNAPSHOT},
    ),
    (
        "reading events instead",
        "hijack_read",
        {"worker_id": "w-1", "hijack_id": "h-1", "mode": "events"},
        True,
        {"events": []},
    ),
    (
        "reading events from a point",
        "hijack_read",
        {"worker_id": "w-1", "hijack_id": "h-1", "mode": "events", "after_seq": 7, "limit": 3},
        True,
        {"events": []},
    ),
    (
        "reading events, which are never cleaned",
        "hijack_read",
        {"worker_id": "w-1", "hijack_id": "h-1", "mode": "events"},
        True,
        {"snapshot": SNAPSHOT},
    ),
    (
        "reading a mode nobody named",
        "hijack_read",
        {"worker_id": "w-1", "hijack_id": "h-1", "mode": "nonsense"},
        True,
        {"snapshot": SNAPSHOT},
    ),
    (
        "reading when the server refused",
        "hijack_read",
        {"worker_id": "w-1", "hijack_id": "h-1"},
        False,
        {"snapshot": SNAPSHOT},
    ),
    (
        "reading a snapshot with no screen in it",
        "hijack_read",
        {"worker_id": "w-1", "hijack_id": "h-1"},
        True,
        {"snapshot": {"cols": 80}},
    ),
    (
        "reading a snapshot that says nothing at all",
        "hijack_read",
        {"worker_id": "w-1", "hijack_id": "h-1"},
        True,
        {"snapshot": {}},
    ),
    (
        "reading a snapshot laid out with no metadata",
        "hijack_read",
        {"worker_id": "w-1", "hijack_id": "h-1", "output": "rendered"},
        True,
        {"snapshot": {"screen": SCREEN}},
    ),
    (
        "reading it as it stands, trimmed to nothing to trim",
        "hijack_read",
        {"worker_id": "w-1", "hijack_id": "h-1", "output": "raw", "tail_lines": 99},
        True,
        {"snapshot": SNAPSHOT},
    ),
    (
        "reading it as it stands, trimmed",
        "hijack_read",
        {"worker_id": "w-1", "hijack_id": "h-1", "output": "raw", "tail_lines": 1},
        True,
        {"snapshot": SNAPSHOT},
    ),
    ("reading with no snapshot at all", "hijack_read", {"worker_id": "w-1", "hijack_id": "h-1"}, True, {"other": 1}),
    ("reading with a bad id", "hijack_read", {"worker_id": "a/b", "hijack_id": "h-1"}, True, {}),
    ("reading a longer wait", "hijack_read", {"worker_id": "w-1", "hijack_id": "h-1", "wait_ms": 50}, True, {}),
    # --- sending -----------------------------------------------------------
    ("sending keys", "hijack_send", {"worker_id": "w-1", "hijack_id": "h-1", "keys": "ls\n"}, True, {"sent": True}),
    (
        "sending keys guarded by a prompt",
        "hijack_send",
        {"worker_id": "w-1", "hijack_id": "h-1", "keys": "y", "expect_prompt_id": "p-1"},
        True,
        {},
    ),
    (
        "sending keys guarded by a pattern",
        "hijack_send",
        {"worker_id": "w-1", "hijack_id": "h-1", "keys": "y", "expect_regex": "^ok$"},
        True,
        {},
    ),
    (
        "sending keys guarded by a pattern that could hang",
        "hijack_send",
        {"worker_id": "w-1", "hijack_id": "h-1", "keys": "y", "expect_regex": "(a+)+$"},
        True,
        {},
    ),
    (
        "sending keys guarded by an enormous pattern",
        "hijack_send",
        {"worker_id": "w-1", "hijack_id": "h-1", "keys": "y", "expect_regex": LONG_PATTERN},
        True,
        {},
    ),
    (
        "sending keys with a bad id and a bad pattern",
        "hijack_send",
        {"worker_id": "a/b", "hijack_id": "h-1", "keys": "y", "expect_regex": "(a+)+"},
        True,
        {},
    ),
    (
        "sending more keys than a worker will take",
        "hijack_send",
        {"worker_id": "w-1", "hijack_id": "h-1", "keys": BIG_KEYS},
        True,
        {},
    ),
    (
        "sending keys with the timings changed",
        "hijack_send",
        {"worker_id": "w-1", "hijack_id": "h-1", "keys": "a", "timeout_ms": 10, "poll_interval_ms": 1},
        True,
        {},
    ),
    ("sending nothing at all", "hijack_send", {"worker_id": "w-1", "hijack_id": "h-1", "keys": ""}, True, {}),
    # --- stepping and letting go -------------------------------------------
    ("stepping", "hijack_step", {"worker_id": "w-1", "hijack_id": "h-1"}, True, {"stepped": True}),
    ("stepping with a bad id", "hijack_step", {"worker_id": "w-1", "hijack_id": "c/d"}, True, {}),
    ("letting go", "hijack_release", {"worker_id": "w-1", "hijack_id": "h-1"}, True, {"released": True}),
    ("letting go with a bad id", "hijack_release", {"worker_id": "a/b", "hijack_id": "h-1"}, True, {}),
    # --- asking the server and the workers ---------------------------------
    ("asking whether the server is well", "server_health", {}, True, {"status": "ok"}),
    ("asking a server that is not", "server_health", {}, False, {"status": "down"}),
    ("setting a session's mode", "session_set_mode", {"session_id": "s-1", "mode": "hijack"}, True, {"mode": "hijack"}),
    (
        "setting the mode of a session that is a path",
        "session_set_mode",
        {"session_id": "a/b", "mode": "open"},
        True,
        {},
    ),
    ("setting a worker's mode", "worker_input_mode", {"worker_id": "w-1", "mode": "open"}, True, {"mode": "open"}),
    (
        "setting the mode of a worker that is a path",
        "worker_input_mode",
        {"worker_id": "a/b", "mode": "open"},
        True,
        {},
    ),
    ("disconnecting a worker", "worker_disconnect", {"worker_id": "w-1"}, True, {"disconnected": True}),
    ("disconnecting a worker that is a path", "worker_disconnect", {"worker_id": "a/b"}, True, {}),
    # --- what an answer is folded into --------------------------------------
    ("an answer that is a list", "server_health", {}, True, [1, 2]),
    ("an answer that is text", "server_health", {}, True, "fine"),
    ("an answer that is nothing", "server_health", {}, True, None),
    ("an answer that is a number", "server_health", {}, True, 7),
    ("an answer already claiming success", "server_health", {}, False, {"success": True, "status": "ok"}),
]


async def _run(tool_name: str, args: dict[str, Any], ok: bool, data: Any) -> dict[str, Any]:
    client = _Client(ok=ok, data=data)
    mcp = MCPServer("differential")
    register_hijack_tools(mcp, client, AuthorizationContext(McpPrincipal(subject_id="u1", roles=frozenset({"admin"}))))
    # MCPServer has no public get_tool(); the ToolManager it delegates to does
    # (mcp._tool_manager.get_tool), matching the design doc's mapping of
    # fastmcp's private _local_provider._components scrape onto the SDK's
    # public ToolManager.get_tool. Synchronous, unlike fastmcp's awaited
    # get_tool() this replaces.
    tool = mcp._tool_manager.get_tool(tool_name)
    assert tool is not None, f"tool {tool_name!r} was not registered"
    return {"result": await tool.fn(**args), "calls": client.calls}


def main() -> None:
    corpus = {
        "max_keystroke_bytes": MAX_KEYSTROKE_BYTES,
        "screen": SCREEN,
        "snapshot": SNAPSHOT,
        "tools": TOOLS,
        "cases": [
            {
                "name": name,
                "tool": tool,
                "args": args,
                "ok": ok,
                "data": data,
                **asyncio.run(_run(tool, args, ok, data)),
            }
            for name, tool, args, ok, data in CASES
        ],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['cases'])} cases)")


if __name__ == "__main__":
    main()
