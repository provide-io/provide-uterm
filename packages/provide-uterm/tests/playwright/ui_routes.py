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


def install_multi_backend_routes(page: Page, frontend_dir: Path | None = None) -> None:
    """Serve /test-page/* and /ui/* from the test runner (backend is wire-only)."""
    fe = frontend_dir or FRONTEND_DIR

    def on_test_page(route: Route) -> None:
        url = route.request.url
        # .../test-page/{worker_id}
        parts = url.rstrip("/").split("/test-page/")
        worker_id = parts[-1].split("?")[0] if len(parts) > 1 else "unknown"
        route.fulfill(status=200, content_type="text/html", body=widget_test_page_html(worker_id))

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

    page.route("**/test-page/**", on_test_page)
    page.route("**/ui/**", on_ui)
