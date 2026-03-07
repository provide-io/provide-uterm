#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""End-to-end tests against the real interactive demo server."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import websockets


def _ws_url(base_url: str, path: str) -> str:
    return base_url.replace("http://", "ws://") + path


async def _drain_until(ws: Any, type_: str, timeout: float = 3.0) -> dict[str, Any] | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
            msg = json.loads(raw)
            if msg.get("type") == type_:
                return msg
        except TimeoutError:
            continue
    return None


async def _wait_for_snapshot_text(ws: Any, needle: str, timeout: float = 3.0) -> dict[str, Any] | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        msg = await _drain_until(ws, "snapshot", timeout=0.5)
        if msg is not None and needle in msg.get("screen", ""):
            return msg
    return None


async def _wait_for_hijack_state(
    ws: Any, *, hijacked: bool | None = None, input_mode: str | None = None, timeout: float = 3.0
) -> dict[str, Any] | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        msg = await _drain_until(ws, "hijack_state", timeout=0.5)
        if msg is None:
            continue
        if hijacked is not None and msg.get("hijacked") is not hijacked:
            continue
        if input_mode is not None and msg.get("input_mode") != input_mode:
            continue
        return msg
    return None


class TestDemoServerWs:
    async def test_demo_worker_announces_hijack_mode_on_connect(self, demo_server: str) -> None:
        async with websockets.connect(_ws_url(demo_server, "/ws/browser/demo-session/term")) as browser:
            hello = await _drain_until(browser, "hello")
            assert hello is not None
            assert hello["worker_online"] is True
            assert hello["input_mode"] == "hijack"

    async def test_input_after_hijack_updates_snapshot(self, demo_server: str) -> None:
        async with httpx.AsyncClient(base_url=demo_server) as http:
            await http.post("/demo/session/demo-session/reset")
        async with websockets.connect(_ws_url(demo_server, "/ws/browser/demo-session/term")) as browser:
            await _drain_until(browser, "hello")
            await _drain_until(browser, "hijack_state")
            await _drain_until(browser, "snapshot")

            await browser.send(json.dumps({"type": "hijack_request"}))
            await _drain_until(browser, "hijack_state")
            await browser.send(json.dumps({"type": "input", "data": "hello from owner"}))

            snapshot = await _wait_for_snapshot_text(browser, "hello from owner")
            assert snapshot is not None
            assert "hello from owner" in snapshot["screen"]
            assert "session: received" in snapshot["screen"]

    async def test_http_mode_switch_updates_browser_to_open_mode(self, demo_server: str) -> None:
        async with httpx.AsyncClient(base_url=demo_server) as http:
            await http.post("/demo/session/demo-session/reset")
        async with websockets.connect(_ws_url(demo_server, "/ws/browser/demo-session/term")) as browser:
            await _drain_until(browser, "hello")
            await _drain_until(browser, "hijack_state")
            await _drain_until(browser, "snapshot")

            async with httpx.AsyncClient(base_url=demo_server) as http:
                resp = await http.post("/demo/session/demo-session/mode", json={"input_mode": "open"})
                assert resp.status_code == 200

            snapshot = await _wait_for_snapshot_text(browser, "Shared input")
            assert snapshot is not None
            assert "Shared input" in snapshot["screen"]

    async def test_switching_to_open_releases_active_hijack_immediately(self, demo_server: str) -> None:
        async with httpx.AsyncClient(base_url=demo_server) as http:
            await http.post("/demo/session/demo-session/reset")
            await http.post("/demo/session/demo-session/mode", json={"input_mode": "hijack"})
        async with (
            websockets.connect(_ws_url(demo_server, "/ws/browser/demo-session/term")) as b1,
            websockets.connect(_ws_url(demo_server, "/ws/browser/demo-session/term")) as b2,
        ):
            for browser in (b1, b2):
                await _drain_until(browser, "hello")
                await _drain_until(browser, "hijack_state")
                await _drain_until(browser, "snapshot")

            await b1.send(json.dumps({"type": "hijack_request"}))
            owner_state = await _drain_until(b1, "hijack_state")
            other_state = await _drain_until(b2, "hijack_state")
            assert owner_state is not None and owner_state["owner"] == "me"
            assert other_state is not None and other_state["owner"] == "other"

            async with httpx.AsyncClient(base_url=demo_server) as http:
                resp = await http.post("/demo/session/demo-session/mode", json={"input_mode": "open"})
                assert resp.status_code == 200

            b1_after = await _wait_for_hijack_state(b1, hijacked=False, input_mode="open")
            b2_after = await _wait_for_hijack_state(b2, hijacked=False, input_mode="open")
            assert b1_after is not None and b1_after["hijacked"] is False
            assert b2_after is not None and b2_after["hijacked"] is False
            assert b1_after["input_mode"] == "open"
            assert b2_after["input_mode"] == "open"

    async def test_two_browsers_can_both_type_in_open_mode(self, demo_server: str) -> None:
        async with httpx.AsyncClient(base_url=demo_server) as http:
            await http.post("/demo/session/demo-session/reset")
            await http.post("/demo/session/demo-session/mode", json={"input_mode": "open"})

        async with (
            websockets.connect(_ws_url(demo_server, "/ws/browser/demo-session/term")) as b1,
            websockets.connect(_ws_url(demo_server, "/ws/browser/demo-session/term")) as b2,
        ):
            for browser in (b1, b2):
                await _drain_until(browser, "hello")
                await _drain_until(browser, "hijack_state")
                await _drain_until(browser, "snapshot")

            await b1.send(json.dumps({"type": "input", "data": "from-one"}))
            snap1 = await _wait_for_snapshot_text(b1, "from-one")
            assert snap1 is not None
            assert "from-one" in snap1["screen"]

            await b2.send(json.dumps({"type": "input", "data": "from-two"}))
            snap2 = await _wait_for_snapshot_text(b2, "from-two")
            assert snap2 is not None
            assert "from-two" in snap2["screen"]

    async def test_hijack_handoff_still_works_in_exclusive_mode(self, demo_server: str) -> None:
        async with httpx.AsyncClient(base_url=demo_server) as http:
            await http.post("/demo/session/demo-session/reset")
            await http.post("/demo/session/demo-session/mode", json={"input_mode": "hijack"})

        async with (
            websockets.connect(_ws_url(demo_server, "/ws/browser/demo-session/term")) as b1,
            websockets.connect(_ws_url(demo_server, "/ws/browser/demo-session/term")) as b2,
        ):
            for browser in (b1, b2):
                await _drain_until(browser, "hello")
                await _drain_until(browser, "hijack_state")
                await _drain_until(browser, "snapshot")

            await b1.send(json.dumps({"type": "hijack_request"}))
            b1_state = await _drain_until(b1, "hijack_state")
            b2_state = await _drain_until(b2, "hijack_state")
            assert b1_state is not None and b1_state["owner"] == "me"
            assert b2_state is not None and b2_state["owner"] == "other"

            await b1.send(json.dumps({"type": "hijack_release"}))
            await _drain_until(b1, "hijack_state")
            await _drain_until(b2, "hijack_state")

            await b2.send(json.dumps({"type": "hijack_request"}))
            b2_state2 = await _drain_until(b2, "hijack_state")
            b1_state2 = await _drain_until(b1, "hijack_state")
            assert b2_state2 is not None and b2_state2["owner"] == "me"
            assert b1_state2 is not None and b1_state2["owner"] == "other"
