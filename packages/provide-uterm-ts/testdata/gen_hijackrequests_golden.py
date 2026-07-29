#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the hijack client's requests.

Every call the client makes is a method, a path and a body, and all three are
part of the wire contract a server was written against. A path built wrongly
reaches the wrong route; a field named wrongly is a field the server ignores,
which looks like the call having no effect.

Two rules run through them:

* **A field nobody set is not sent.** An optional expectation left out is
  absent from the body rather than present and null, because a server reading
  a null may take it for an instruction.
* **Every answer is a pair.** Whether it worked, and the body — a failed call
  hands back what the server said rather than raising, so a caller can show
  it.

Driven: the real client, with its transport replaced by a recorder.

# uv-package: provide-uterm-client

Usage (from the repository root)::

    uv run --package provide-uterm-client python \\
        packages/provide-uterm-ts/testdata/gen_hijackrequests_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.client.hijack import HijackClient

OUT = Path(__file__).resolve().parent / "hijackrequests_golden.json"


class RecordingResponse:
    """A response the recorder hands back."""

    def __init__(self, status: int, body: Any, *, no_json: bool = False) -> None:
        self.status_code = status
        self._body = body
        self._no_json = no_json
        self.text = "" if no_json else json.dumps(body)

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        if self._no_json:
            raise ValueError("no json")
        return self._body


class RecordingTransport:
    """Stands in for the HTTP client, writing down what it was asked for."""

    def __init__(self, status: int = 200, body: Any = None, *, no_json: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._status = status
        # `None` is a body somebody might mean, so "there is none" is said
        # separately rather than smuggled through the value.
        self._body = None if no_json else (body if body is not None else {"ok": True})
        self._no_json = no_json

    async def request(self, method: str, path: str, **kwargs: Any) -> RecordingResponse:
        self.calls.append({"method": method, "path": path, "json": kwargs.get("json"), "params": kwargs.get("params")})
        return RecordingResponse(self._status, self._body, no_json=self._no_json)


def _client(transport: RecordingTransport) -> HijackClient:
    client = HijackClient(base_url="http://server.test")
    client._client = transport  # type: ignore[assignment]
    return client


async def _call(
    name: str, invoke: Any, *, status: int = 200, body: Any = None, no_json: bool = False
) -> dict[str, Any]:
    transport = RecordingTransport(status, body, no_json=no_json)
    client = _client(transport)
    ok, answer = await invoke(client)
    return {
        "name": name,
        "calls": transport.calls,
        "ok": ok,
        "answer": answer,
        "status": status,
        "no_json": no_json,
    }


async def main_async() -> None:
    calls: list[dict[str, Any]] = [
        await _call("acquiring a hijack", lambda c: c.acquire("w1")),
        await _call("acquiring with an owner and a lease", lambda c: c.acquire("w1", owner="ada", lease_s=30)),
        await _call("a heartbeat", lambda c: c.heartbeat("w1", "h1")),
        await _call("a heartbeat with a lease", lambda c: c.heartbeat("w1", "h1", lease_s=15)),
        await _call("sending keys", lambda c: c.send("w1", "h1", keys="ls\n")),
        await _call(
            "sending keys with an expected prompt",
            lambda c: c.send("w1", "h1", keys="ls\n", expect_prompt_id="p1"),
        ),
        await _call(
            "sending keys with an expected pattern",
            lambda c: c.send("w1", "h1", keys="ls\n", expect_regex="^\\$ "),
        ),
        await _call(
            "sending keys with both expectations and timings",
            lambda c: c.send(
                "w1", "h1", keys="ls\n", expect_prompt_id="p1", expect_regex="x", timeout_ms=50, poll_interval_ms=5
            ),
        ),
        await _call("stepping", lambda c: c.step("w1", "h1")),
        await _call("releasing", lambda c: c.release("w1", "h1")),
        await _call("a snapshot", lambda c: c.snapshot("w1", "h1")),
        await _call("a snapshot with a wait", lambda c: c.snapshot("w1", "h1", wait_ms=10)),
        await _call("events", lambda c: c.events("w1", "h1")),
        await _call("events after a sequence", lambda c: c.events("w1", "h1", after_seq=7, limit=5)),
        await _call("a screenshot", lambda c: c.gui_screenshot("w1", "h1")),
        await _call("a click", lambda c: c.gui_click("w1", "h1", 10, 20)),
        await _call("a right click", lambda c: c.gui_click("w1", "h1", 10, 20, button="right")),
        await _call("typing", lambda c: c.gui_type("w1", "h1", "hello")),
        await _call("a key", lambda c: c.gui_key("w1", "h1", "Return")),
        await _call("a drag", lambda c: c.gui_drag("w1", "h1", 1, 2, 3, 4)),
        await _call("setting the input mode", lambda c: c.set_input_mode("w1", "hijack")),
        await _call("disconnecting a worker", lambda c: c.disconnect_worker("w1")),
        await _call("health", lambda c: c.health()),
        await _call("listing sessions", lambda c: c.list_sessions()),
        await _call("one session", lambda c: c.get_session("sess-1")),
        await _call("a session snapshot", lambda c: c.session_snapshot("sess-1")),
        # Outcomes
        await _call("a refusal", lambda c: c.step("w1", "h1"), status=409, body={"error": "held by somebody else"}),
        await _call("a server fault", lambda c: c.step("w1", "h1"), status=500, body={"error": "broken"}),
        await _call("an answer that is not json", lambda c: c.step("w1", "h1"), status=200, no_json=True),
        await _call("a created answer", lambda c: c.acquire("w1"), status=201, body={"hijack_id": "h1"}),
        await _call("an answer with no content", lambda c: c.release("w1", "h1"), status=204, body={"released": True}),
    ]

    corpus = {"calls": calls}
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(calls)} calls)")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
