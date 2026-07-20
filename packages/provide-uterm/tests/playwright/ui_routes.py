#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""page.route helpers for multi-backend Playwright — serve test HTML + /ui assets."""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path

from playwright.sync_api import Page, Route

from .backend_server import FRONTEND_DIR

_DEEP_QUERY = """
window.__deepQuery=(sel)=>{const s=(r)=>{const d=r.querySelector(sel);if(d)return d;
for(const e of r.querySelectorAll('*')){if(e.shadowRoot){const f=s(e.shadowRoot);if(f)return f;}}return null;};
return s(document);};
window.__deepQueryAll=(sel)=>{const o=[];const s=(r)=>{for(const e of r.querySelectorAll(sel))o.push(e);
for(const e of r.querySelectorAll('*')){if(e.shadowRoot)s(e.shadowRoot);}};s(document);return o;};
"""


def _resolve_script() -> str:
    try:
        from provide.uterm.server.ui import _resolve_vanilla_asset

        return _resolve_vanilla_asset("src/hijack.ts")
    except Exception:
        # Built frontend may expose hashed path; fall back to common entry.
        return "src/hijack.ts"


def widget_test_page_html(worker_id: str, *, heartbeat_ms: int = 500) -> str:
    script_path = _resolve_script()
    return (
        "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
        f"<script>{_DEEP_QUERY}</script>"
        "<style>*{margin:0;padding:0;box-sizing:border-box}"
        "html,body{width:100%;height:100dvh;background:#0b0f14}"
        "#app,uterm-session{display:block;width:100%;height:100%}</style></head>"
        "<body><div id='app'></div>"
        "<script type='module'>"
        f"import '/ui/{script_path}';"
        "customElements.whenDefined('uterm-session').then(() => {"
        "  const el = document.createElement('uterm-session');"
        "  el.id = 'app-root';"
        f"  el.config = {{workerId:{json.dumps(worker_id)},heartbeatInterval:{heartbeat_ms}}};"
        "  document.getElementById('app').appendChild(el);"
        "  el.connect();"
        "  window.demoHijack = el;"
        "});"
        "</script>"
        "</body></html>"
    )


def deckmux_test_page_html(worker_id: str) -> str:
    """Minimal HTML: DeckMux presence UI over /ws/browser/{id}/term (all backends)."""
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
    el.title=(u.name||uid)+" ("+(u.role||"?")+")";el.setAttribute("data-user-id",uid);
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
    else{{ci.textContent="Control: "+window._controlHolder;
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
    if(msg.users&&msg.users.length>0&&!window._myUserId)window._myUserId=msg.users[msg.users.length-1].user_id;
    window._presenceSynced=true;render();
  }}else if(msg.type==="presence_update"){{
    if(msg.user_id)window._users[msg.user_id]=Object.assign({{}},window._users[msg.user_id]||{{}},msg);render();
  }}else if(msg.type==="presence_leave"){{
    delete window._users[msg.user_id];render();
  }}else if(msg.type==="control_transfer"){{
    window._controlHolder=msg.to_user_id||null;render();
  }}
}});}};
ws.onerror=function(e){{console.error("WS error",e);}};
}})();</script></body></html>"""


def spinner_mock_page_html(worker_id: str, *, heartbeat_ms: int = 500) -> str:
    """Mock-xterm reconnect/spinner test page (exposes window._widget / _termWrites)."""
    script_path = _resolve_script()
    mock_js = """
<script>
window._termWrites = [];
window.Terminal = class MockTerminal {
  constructor(opts) { this._onDataCb = null; }
  open(el) {}
  focus() {}
  write(data) { window._termWrites.push(data); }
  reset() { window._termWrites.push('\\x00RESET\\x00'); }
  loadAddon(addon) {}
  onData(cb) { this._onDataCb = cb; window._onDataCb = cb; }
  dispose() {}
};
window.FitAddon = { FitAddon: class MockFitAddon { fit() {} } };
</script>
"""
    return (
        "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
        f"<script>{_DEEP_QUERY}</script>"
        "<style>*{margin:0;padding:0;box-sizing:border-box}"
        "html,body{width:100%;height:100dvh;background:#0b0f14}"
        "#app,uterm-session{display:block;width:100%;height:100%}</style></head>"
        "<body><div id='app'></div>"
        f"{mock_js}"
        "<script type='module'>"
        f"import '/ui/{script_path}';"
        "customElements.whenDefined('uterm-session').then(() => {"
        "  const w = document.createElement('uterm-session');"
        "  w.id = 'app-root';"
        f"  w.config = {{workerId:{json.dumps(worker_id)},heartbeatInterval:{heartbeat_ms}}};"
        "  document.getElementById('app').appendChild(w);"
        "  w.connect();"
        "  window._widget = w;"
        "  window.__hijackTestHooks = {"
        "    startReconnectAnim: () => window.__testHooks_startReconnectAnim(w._hijackState),"
        "    stopReconnectAnim: () => window.__testHooks_stopReconnectAnim(w._hijackState),"
        "  };"
        "});"
        "</script>"
        "</body></html>"
    )


def install_multi_backend_routes(
    page: Page,
    frontend_dir: Path | None = None,
    *,
    spinner_mock: bool = False,
) -> None:
    """Serve /test-page/*, /deckmux-test/*, and /ui/* from the test runner.

    Idempotent per Page: re-entry after rapid refresh / multi-tab chaos is a no-op.
    When *spinner_mock* is true, /test-page uses the mock-xterm reconnect harness.
    """
    if getattr(page, "_uterm_mb_routes", False):
        return
    fe = frontend_dir or FRONTEND_DIR

    def on_test_page(route: Route) -> None:
        url = route.request.url
        parts = url.rstrip("/").split("/test-page/")
        worker_id = parts[-1].split("?")[0] if len(parts) > 1 else "unknown"
        html = spinner_mock_page_html(worker_id) if spinner_mock else widget_test_page_html(worker_id)
        route.fulfill(status=200, content_type="text/html", body=html)

    def on_deckmux_page(route: Route) -> None:
        url = route.request.url
        parts = url.rstrip("/").split("/deckmux-test/")
        worker_id = parts[-1].split("?")[0] if len(parts) > 1 else "unknown"
        route.fulfill(status=200, content_type="text/html", body=deckmux_test_page_html(worker_id))

    def on_ui(route: Route) -> None:
        url = route.request.url
        rel = url.split("/ui/", 1)[-1].split("?")[0]
        path = (fe / rel).resolve()
        try:
            path.relative_to(fe.resolve())
        except ValueError:
            route.fulfill(status=403, body="forbidden")
            return
        if not path.is_file():
            route.fulfill(status=404, body=f"missing {rel}")
            return
        mime, _ = mimetypes.guess_type(str(path))
        route.fulfill(
            status=200,
            content_type=mime or "application/octet-stream",
            body=path.read_bytes(),
        )

    def on_color_page(route: Route) -> None:
        url = route.request.url
        parts = url.rstrip("/").split("/color-test/")
        worker_id = parts[-1].split("?")[0] if len(parts) > 1 else "unknown"
        route.fulfill(status=200, content_type="text/html", body=color_test_page_html(worker_id))

    page.route("**/test-page/**", on_test_page)
    page.route("**/deckmux-test/**", on_deckmux_page)
    page.route("**/color-test/**", on_color_page)
    page.route("**/ui/**", on_ui)
    page._uterm_mb_routes = True


def multi_backend_env() -> bool:
    """True when Playwright should use real language-server subprocesses."""
    import os

    multi = os.environ.get("UTERM_MULTI_BACKEND", "").strip().lower() in ("1", "true", "yes")
    backend = os.environ.get("UTERM_TEST_BACKEND", "python").strip().lower() or "python"
    return multi or backend in ("go", "csharp")


def color_test_page_html(worker_id: str) -> str:
    """xterm.js color test page — same contract as playwright/conftest._color_test_html."""
    xterm = "https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0"
    fit = "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Color Palette Test</title>
  <link rel="stylesheet" href="{xterm}/css/xterm.css">
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    html, body {{ width: 100%; height: 100vh; background: #0b0f14; }}
    #term {{ width: 100%; height: 100%; }}
  </style>
</head>
<body>
<div id="term"></div>
<script src="{xterm}/lib/xterm.js"></script>
<script src="{fit}/lib/addon-fit.js"></script>
<script>
(function() {{
  var DLE = "\\x10", STX = "\\x02";
  var term = new Terminal({{ rows: 40, cols: 120, convertEol: false }});
  term.open(document.getElementById("term"));
  var fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  try {{ fitAddon.fit(); }} catch(e) {{}}
  window._term = term;
  window._colorDataReceived = false;

  var proto = location.protocol === "https:" ? "wss:" : "ws:";
  var wsUrl = proto + "//" + location.host + "/ws/browser/{worker_id}/term";
  var ws = new WebSocket(wsUrl);

  ws.onmessage = function(event) {{
    var raw = event.data;
    var pos = 0;
    while (pos < raw.length) {{
      if (raw[pos] === DLE && pos + 1 < raw.length && raw[pos+1] === STX) {{
        var lenHex = raw.substring(pos + 2, pos + 10);
        var jsonLen = parseInt(lenHex, 16);
        var jsonStart = pos + 11;
        var jsonStr = raw.substring(jsonStart, jsonStart + jsonLen);
        try {{
          var msg = JSON.parse(jsonStr);
          if (msg.type === "term" && msg.data) {{
            term.write(msg.data);
            window._colorDataReceived = true;
          }} else if (msg.type === "snapshot" && msg.screen) {{
            term.write(msg.screen.replace(/\\n/g, "\\r\\n"));
            window._colorDataReceived = true;
          }}
        }} catch(e) {{}}
        pos = jsonStart + jsonLen;
      }} else {{
        var next = raw.indexOf(DLE, pos + 1);
        if (next === -1) next = raw.length;
        var chunk = raw.substring(pos, next);
        if (chunk) {{
          term.write(chunk);
          window._colorDataReceived = true;
        }}
        pos = next;
      }}
    }}
  }};

  ws.onopen = function() {{ window._wsConnected = true; }};
  ws.onerror = function(e) {{ console.error("WS error", e); }};
}})();
</script>
</body>
</html>"""
