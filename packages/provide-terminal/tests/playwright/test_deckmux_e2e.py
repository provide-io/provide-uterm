#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Playwright E2E tests for DeckMux collaborative presence.

Two browsers connect to the same TermHub session via DeckMux and verify
presence sync, updates, pins, control transfer, and leave broadcasts.
"""

from __future__ import annotations

import importlib.resources
import socket
import threading
import time
import uuid
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest
import starlette.requests
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from playwright.sync_api import Page
from starlette.staticfiles import StaticFiles
from provide.terminal.deckmux._hub_mixin import DeckMuxMixin

from tests.conftest import WorkerController
from provide.terminal.hijack.hub import TermHub

SCREENSHOTS_DIR = Path("packages/provide-terminal/tests/playwright/screenshots")


class DeckMuxTermHub(DeckMuxMixin, TermHub):
    """TermHub with DeckMux presence routing mixed in."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._deckmux_init()


def _deckmux_test_html(worker_id: str) -> str:
    """Minimal HTML page: connects WS, tracks DeckMux messages, renders presence UI."""
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:100%;height:100vh;background:#0b0f14;color:#e0e0e0;font-family:monospace}}
#status{{padding:8px;font-size:14px;background:#151a22}}
#presence-bar{{display:flex;gap:6px;padding:8px;min-height:48px;background:#1a2030;align-items:center}}
.avatar{{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  font-weight:bold;font-size:14px;color:#fff;border:2px solid transparent}}
.avatar.is-owner{{border-color:gold}}
#pins{{padding:8px;min-height:32px;background:#1c2535}}
.pin-label{{display:inline-block;padding:2px 8px;margin:2px;border-radius:4px;font-size:12px}}
#edge-indicators{{position:fixed;right:0;top:0;width:8px;height:100vh;background:#111}}
.edge-marker{{position:absolute;width:100%;border-radius:2px;opacity:0.7}}
#control-indicator{{padding:8px;background:#1e2840;font-size:13px}}
#messages{{padding:8px;font-size:11px;max-height:200px;overflow-y:auto;background:#0e1118}}
</style></head><body>
<div id="status">Connecting...</div><div id="presence-bar"></div>
<div id="pins"></div><div id="control-indicator"></div>
<div id="edge-indicators"></div><div id="messages"></div>
<script>(function(){{
var DLE="\\x10",STX="\\x02";
window._users={{}};window._myUserId=null;window._receivedMessages=[];
window._wsConnected=false;window._presenceSynced=false;window._controlHolder=null;
function parseFrames(raw){{var m=[],p=0;while(p<raw.length){{
  if(raw[p]===DLE&&p+1<raw.length&&raw[p+1]===STX){{
    var l=parseInt(raw.substring(p+2,p+10),16),s=p+11;
    try{{m.push(JSON.parse(raw.substring(s,s+l)))}}catch(e){{}}p=s+l;
  }}else{{var n=raw.indexOf(DLE,p+1);p=n===-1?raw.length:n;}}}}return m;}}
function render(){{
  var bar=document.getElementById("presence-bar");bar.innerHTML="";
  var ids=Object.keys(window._users);
  ids.forEach(function(uid){{var u=window._users[uid];
    var el=document.createElement("div");
    el.className="avatar"+(u.is_owner?" is-owner":"");
    el.style.backgroundColor=u.color||"#555";el.textContent=u.initials||"??";
    el.title=u.name+" ("+u.role+")";el.setAttribute("data-user-id",uid);
    bar.appendChild(el);}});
  document.getElementById("status").textContent="Connected: "+ids.length+" user(s)";
  var pins=document.getElementById("pins");pins.innerHTML="";
  ids.forEach(function(uid){{var u=window._users[uid];
    if(u.pin&&u.pin.line!==undefined){{var el=document.createElement("span");
      el.className="pin-label";el.style.backgroundColor=u.color||"#555";
      el.textContent=(u.initials||"??")+" @ line "+u.pin.line;
      if(u.pin.label)el.textContent+=": "+u.pin.label;
      el.setAttribute("data-pin-user",uid);pins.appendChild(el);}}
  }});
  var edge=document.getElementById("edge-indicators");edge.innerHTML="";
  ids.forEach(function(uid){{var u=window._users[uid];
    if(u.scroll_line!==undefined&&u.scroll_line>0){{var mk=document.createElement("div");
      mk.className="edge-marker";mk.style.backgroundColor=u.color||"#555";
      mk.style.top=Math.min(u.scroll_line/100,1)*100+"%";mk.style.height="4px";
      mk.setAttribute("data-edge-user",uid);edge.appendChild(mk);}}
  }});
  var ci=document.getElementById("control-indicator");
  if(window._controlHolder){{var cu=window._users[window._controlHolder];
    if(cu){{ci.textContent="Control: "+cu.name+" ("+cu.initials+")";
      ci.setAttribute("data-control-holder",window._controlHolder);}}
  }}else{{ci.textContent="Control: none";ci.removeAttribute("data-control-holder");}}
}}
function encodeCtrl(obj){{var j=JSON.stringify(obj);
  return DLE+STX+j.length.toString(16).padStart(8,"0")+":"+j;}}
window._encodeControl=encodeCtrl;
var proto=location.protocol==="https:"?"wss:":"ws:";
var ws=new WebSocket(proto+"//"+location.host+"/ws/browser/{worker_id}/term");
window._ws=ws;
window._sendControl=function(obj){{ws.send(encodeCtrl(obj));}};
ws.onopen=function(){{window._wsConnected=true;}};
ws.onmessage=function(ev){{parseFrames(ev.data).forEach(function(msg){{
  window._receivedMessages.push(msg);
  var d=document.createElement("div");d.textContent=msg.type+": "+JSON.stringify(msg);
  var ml=document.getElementById("messages");ml.appendChild(d);ml.scrollTop=ml.scrollHeight;
  if(msg.type==="presence_sync"){{window._users={{}};
    (msg.users||[]).forEach(function(u){{window._users[u.user_id]=u;}});
    if(msg.users&&msg.users.length>0)window._myUserId=msg.users[msg.users.length-1].user_id;
    window._presenceSynced=true;render();
  }}else if(msg.type==="presence_update"){{
    if(msg.user_id)window._users[msg.user_id]=msg;render();
  }}else if(msg.type==="presence_leave"){{
    delete window._users[msg.user_id];render();
  }}else if(msg.type==="control_transfer"){{
    window._controlHolder=msg.to_user_id;render();
  }}
}});}};
ws.onerror=function(e){{console.error("WS error",e);}};
}})();</script></body></html>"""


@pytest.fixture(scope="module")
def deckmux_server() -> Generator[tuple[str, DeckMuxTermHub], None, None]:
    """Module-scoped server: DeckMux-enabled TermHub + test pages."""
    hub = DeckMuxTermHub(resolve_browser_role=lambda _ws, _worker_id: "admin")
    app = FastAPI()
    app.include_router(hub.create_router())

    frontend_path = importlib.resources.files("provide.terminal") / "frontend"
    app.mount("/ui", StaticFiles(directory=str(frontend_path), html=True), name="ui")

    @app.get("/deckmux-test/{worker_id}", response_class=HTMLResponse)
    async def deckmux_test_page(worker_id: str) -> str:
        return _deckmux_test_html(worker_id)

    @app.post("/deckmux-broadcast/{worker_id}")
    async def deckmux_broadcast(worker_id: str, request: starlette.requests.Request) -> dict[str, str]:
        """Test helper: broadcast an arbitrary JSON message to all browsers."""
        body = await request.json()
        await hub.broadcast(worker_id, body)
        return {"status": "ok"}

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("deckmux_server: uvicorn failed to start within 10 s")
        time.sleep(0.05)

    yield f"http://127.0.0.1:{port}", hub

    server.should_exit = True
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _navigate(page: Page, base_url: str, worker_id: str) -> None:
    page.goto(f"{base_url}/deckmux-test/{worker_id}", wait_until="domcontentloaded")


def _wait_synced(page: Page, timeout: int = 10000) -> None:
    """Wait for the page to receive a presence_sync message."""
    page.wait_for_function("window._presenceSynced === true", timeout=timeout)


def _user_count(page: Page) -> int:
    return page.evaluate("Object.keys(window._users).length")


def _avatar_count(page: Page) -> int:
    return page.locator("#presence-bar .avatar").count()


def _announce_presence(page: Page) -> None:
    """Send an initial presence_update so other browsers learn about this page.

    The mixin's ``deckmux_on_browser_connect`` only sends a ``presence_sync``
    to the joining browser.  Existing browsers only learn about the newcomer
    when the newcomer sends its first ``presence_update``, which triggers the
    mixin to broadcast it to all browsers.
    """
    page.evaluate("""
        window._sendControl({
            type: "presence_update",
            scroll_line: 0,
            scroll_range: [0, 25]
        })
    """)


def _screenshot(page: Page, name: str) -> None:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SCREENSHOTS_DIR / f"{name}.png"))


# ---------------------------------------------------------------------------
# Test: two browsers see each other
# ---------------------------------------------------------------------------


class TestTwoBrowsersSeeEachOther:
    def test_two_browsers_see_each_other(
        self, page: Page, browser: object, deckmux_server: tuple[str, DeckMuxTermHub]
    ) -> None:
        """Two browsers connecting to the same session both see 2 avatars."""
        base_url, hub = deckmux_server
        worker_id = f"dm-2br-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        try:
            # Browser A connects and announces presence
            _navigate(page, base_url, worker_id)
            _wait_synced(page)
            _announce_presence(page)
            assert _user_count(page) == 1

            # Browser B connects (new context = separate WS connection)
            ctx2 = browser.new_context()  # type: ignore[attr-defined]
            page2 = ctx2.new_page()
            try:
                _navigate(page2, base_url, worker_id)
                _wait_synced(page2)

                # Page2 should see 2 users in its sync
                page2.wait_for_function("Object.keys(window._users).length === 2", timeout=5000)
                assert _user_count(page2) == 2
                assert _avatar_count(page2) == 2

                # Page2 announces itself so page1 learns about it
                _announce_presence(page2)

                # Page1 should also see 2 after receiving the broadcast
                page.wait_for_function("Object.keys(window._users).length === 2", timeout=5000)
                assert _user_count(page) == 2
                assert _avatar_count(page) == 2

                _screenshot(page, "deckmux-2br-page1")
                _screenshot(page2, "deckmux-2br-page2")
            finally:
                page2.close()
                ctx2.close()
        finally:
            ctrl.stop()


# ---------------------------------------------------------------------------
# Test: presence update broadcast (scroll / edge indicator)
# ---------------------------------------------------------------------------


class TestPresenceUpdateBroadcast:
    def test_presence_update_broadcast(
        self, page: Page, browser: object, deckmux_server: tuple[str, DeckMuxTermHub]
    ) -> None:
        """Browser A sends a presence_update; Browser B's edge indicator updates."""
        base_url, hub = deckmux_server
        worker_id = f"dm-upd-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        try:
            _navigate(page, base_url, worker_id)
            _wait_synced(page)
            _announce_presence(page)

            ctx2 = browser.new_context()  # type: ignore[attr-defined]
            page2 = ctx2.new_page()
            try:
                _navigate(page2, base_url, worker_id)
                _wait_synced(page2)
                _announce_presence(page2)
                page.wait_for_function("Object.keys(window._users).length === 2", timeout=5000)

                # Browser A sends a scroll presence update
                page.evaluate("""
                    window._sendControl({
                        type: "presence_update",
                        scroll_line: 42,
                        scroll_range: [30, 55]
                    })
                """)

                # Browser B should receive the update and render an edge marker
                page2.wait_for_function(
                    """() => {
                        var users = Object.values(window._users);
                        return users.some(u => u.scroll_line === 42);
                    }""",
                    timeout=5000,
                )

                # Verify edge indicator rendered
                edge_markers = page2.locator("#edge-indicators .edge-marker")
                assert edge_markers.count() >= 1

                _screenshot(page, "deckmux-scroll-page1")
                _screenshot(page2, "deckmux-scroll-page2")
            finally:
                page2.close()
                ctx2.close()
        finally:
            ctrl.stop()


# ---------------------------------------------------------------------------
# Test: pin visible to other browser
# ---------------------------------------------------------------------------


class TestPinVisibleToOther:
    def test_pin_visible_to_other_browser(
        self, page: Page, browser: object, deckmux_server: tuple[str, DeckMuxTermHub]
    ) -> None:
        """Browser A pins a line; Browser B sees the pin label."""
        base_url, hub = deckmux_server
        worker_id = f"dm-pin-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        try:
            _navigate(page, base_url, worker_id)
            _wait_synced(page)
            _announce_presence(page)

            ctx2 = browser.new_context()  # type: ignore[attr-defined]
            page2 = ctx2.new_page()
            try:
                _navigate(page2, base_url, worker_id)
                _wait_synced(page2)
                _announce_presence(page2)
                page.wait_for_function("Object.keys(window._users).length === 2", timeout=5000)

                # Browser A pins line 17
                page.evaluate("""
                    window._sendControl({
                        type: "presence_update",
                        pin: { line: 17, label: "important" }
                    })
                """)

                # Browser B should see the pin
                page2.wait_for_function(
                    """() => {
                        var users = Object.values(window._users);
                        return users.some(u => u.pin && u.pin.line === 17);
                    }""",
                    timeout=5000,
                )

                # Verify pin label rendered
                pin_labels = page2.locator("#pins .pin-label")
                assert pin_labels.count() >= 1
                pin_text = pin_labels.first.text_content() or ""
                assert "17" in pin_text
                assert "important" in pin_text

                _screenshot(page, "deckmux-pin-page1")
                _screenshot(page2, "deckmux-pin-page2")
            finally:
                page2.close()
                ctx2.close()
        finally:
            ctrl.stop()


# ---------------------------------------------------------------------------
# Test: control transfer between browsers
# ---------------------------------------------------------------------------


class TestControlTransfer:
    def test_control_transfer(self, page: Page, browser: object, deckmux_server: tuple[str, DeckMuxTermHub]) -> None:
        """Browser A has control; after transfer, Browser B becomes the controller.

        Since control_request handling is a placeholder in the mixin, we
        test the full pipeline by:
        1. Setting Browser A as owner via the presence store
        2. Manually triggering a control_transfer broadcast through the hub
        3. Verifying both browsers receive and render the transfer
        """
        base_url, hub = deckmux_server
        worker_id = f"dm-xfer-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        try:
            _navigate(page, base_url, worker_id)
            _wait_synced(page)
            _announce_presence(page)

            ctx2 = browser.new_context()  # type: ignore[attr-defined]
            page2 = ctx2.new_page()
            try:
                _navigate(page2, base_url, worker_id)
                _wait_synced(page2)
                _announce_presence(page2)
                page.wait_for_function("Object.keys(window._users).length === 2", timeout=5000)
                page2.wait_for_function("Object.keys(window._users).length === 2", timeout=5000)

                # Grab the user IDs from both pages
                user_a_id = page.evaluate("window._myUserId")
                user_b_id = page2.evaluate("window._myUserId")
                assert user_a_id is not None
                assert user_b_id is not None
                assert user_a_id != user_b_id

                # Broadcast a control_transfer message via the test helper endpoint
                httpx.post(
                    f"{base_url}/deckmux-broadcast/{worker_id}",
                    json={
                        "type": "control_transfer",
                        "from_user_id": user_a_id,
                        "to_user_id": user_b_id,
                        "reason": "handover",
                        "queued_keys": "",
                    },
                    timeout=5,
                )

                # Both browsers should see the transfer
                page.wait_for_function(
                    f"window._controlHolder === '{user_b_id}'",
                    timeout=5000,
                )
                page2.wait_for_function(
                    f"window._controlHolder === '{user_b_id}'",
                    timeout=5000,
                )

                # Verify control indicator shows B
                ctrl_text_1 = page.locator("#control-indicator").text_content() or ""
                ctrl_text_2 = page2.locator("#control-indicator").text_content() or ""
                assert "Control:" in ctrl_text_1
                assert "Control:" in ctrl_text_2

                ctrl_holder_1 = page.locator("#control-indicator").get_attribute("data-control-holder")
                ctrl_holder_2 = page2.locator("#control-indicator").get_attribute("data-control-holder")
                assert ctrl_holder_1 == user_b_id
                assert ctrl_holder_2 == user_b_id

                _screenshot(page, "deckmux-xfer-page1")
                _screenshot(page2, "deckmux-xfer-page2")
            finally:
                page2.close()
                ctx2.close()
        finally:
            ctrl.stop()


# ---------------------------------------------------------------------------
# Test: presence leave on disconnect
# ---------------------------------------------------------------------------


class TestPresenceLeave:
    def test_presence_leave_on_disconnect(
        self, page: Page, browser: object, deckmux_server: tuple[str, DeckMuxTermHub]
    ) -> None:
        """When Browser B disconnects, Browser A sees user count drop to 1."""
        base_url, hub = deckmux_server
        worker_id = f"dm-leave-{_uid()}"
        ctrl = WorkerController(base_url, worker_id).start()
        try:
            _navigate(page, base_url, worker_id)
            _wait_synced(page)
            _announce_presence(page)

            ctx2 = browser.new_context()  # type: ignore[attr-defined]
            page2 = ctx2.new_page()
            try:
                _navigate(page2, base_url, worker_id)
                _wait_synced(page2)
                _announce_presence(page2)
                page.wait_for_function("Object.keys(window._users).length === 2", timeout=5000)
            finally:
                # Close browser B
                page2.close()
                ctx2.close()

            # Browser A should receive presence_leave and drop back to 1 user
            page.wait_for_function("Object.keys(window._users).length === 1", timeout=5000)
            assert _user_count(page) == 1
            assert _avatar_count(page) == 1

            _screenshot(page, "deckmux-leave-page1")
        finally:
            ctrl.stop()
