#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Multi-session and fleet browser recording for demo recording scripts."""

from __future__ import annotations

import contextlib
import math
import shutil
import time
from typing import TYPE_CHECKING, Any

from scripts.demos.browser import _dev_auth_cookies_or_empty
from scripts.demos.ffmpeg import ffmpeg_to_mp4
from scripts.demos.grid_compose import tile_grid_with_footer

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from playwright.sync_api import Page

    from scripts.demos.server import BrowserStep

_FLEET_COLS = 3  # columns in fleet grid (3x1 for 3 sessions, 3x3 for 9)


def _header_mode_cookies(context_extra: dict[str, Any], base_url: str) -> list[dict[str, str]]:
    """Mirror header-mode ``X-Uterm-Principal``/``Role`` into cookies.

    Playwright does not forward a context's ``extra_http_headers`` on the
    page-initiated WebSocket handshake, so the in-page hijack WS would
    authenticate as an anonymous viewer and silently drop input in open mode
    (only operators/admins may send). Cookies *are* carried on the WS
    handshake, so replay the same identity there. No-op when the context sets
    no ``X-Uterm-*`` headers (e.g. the dev_token recorders).
    """
    headers = context_extra.get("extra_http_headers") or {}
    cookies: list[dict[str, str]] = []
    for header_name, cookie_name in (("X-Uterm-Principal", "uterm_principal"), ("X-Uterm-Role", "uterm_role")):
        value = headers.get(header_name)
        if value:
            cookies.append({"name": cookie_name, "value": value, "url": base_url})
    return cookies


def record_simultaneous_perspectives(
    perspectives: dict[str, list[BrowserStep]],
    base_url: str,
    feature_dir: Path,
    *,
    context_options: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Path | None]:
    """Record multiple named browser perspectives with ALL contexts open simultaneously.

    Steps are interleaved: step[i] runs for every perspective before any
    advances to step[i+1]. This ensures all browser windows are live at the
    same time, which is required for multi-user features (presence cursors,
    simultaneous broadcasts, hijack viewer/operator interaction).

    Each perspective gets its own <name>.mp4 in feature_dir.
    Pass context_options={name: {extra_http_headers: {...}, ...}} to customise
    individual Playwright browser contexts (e.g. for DeckMux display names).
    Returns {name: mp4_path_or_None}.
    """
    from playwright.sync_api import sync_playwright

    shots_dir = feature_dir / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    names = list(perspectives.keys())

    def _resolve(steps: list[BrowserStep]) -> list[BrowserStep]:
        return [(base_url + a if isinstance(a, str) and a.startswith("/") else a, w, s) for a, w, s in steps]

    resolved = {name: _resolve(steps) for name, steps in perspectives.items()}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            webm_dirs: dict[str, Path] = {}
            contexts: dict[str, Any] = {}
            pages: dict[str, Any] = {}
            # Browser navigations / page-driven WS carry no bearer header — they
            # authenticate via the uterm_token cookie (dev_token mode). Without
            # this every /app/session/* load 401s and the video records an error.
            auth_cookies = _dev_auth_cookies_or_empty(base_url)
            for name in names:
                wd = feature_dir / f"_webm_{name}"
                wd.mkdir(parents=True, exist_ok=True)
                webm_dirs[name] = wd
                extra = (context_options or {}).get(name, {})
                ctx = browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    record_video_dir=str(wd),
                    record_video_size={"width": 1280, "height": 720},
                    **extra,
                )
                if auth_cookies:
                    ctx.add_cookies(auth_cookies)  # type: ignore[arg-type]
                persona_cookies = _header_mode_cookies(extra, base_url)
                if persona_cookies:
                    ctx.add_cookies(persona_cookies)  # type: ignore[arg-type]
                contexts[name] = ctx
                pages[name] = ctx.new_page()

            max_steps = max(len(s) for s in resolved.values())
            for i in range(max_steps):
                for name in names:
                    steps = resolved[name]
                    if i >= len(steps):
                        continue
                    action, wait_s, shot_name = steps[i]
                    page = pages[name]
                    if isinstance(action, str):
                        page.goto(action)
                        page.wait_for_load_state("networkidle")
                    elif callable(action):
                        action(page)
                    if wait_s > 0:
                        time.sleep(wait_s)
                    if shot_name:
                        page.screenshot(path=str(shots_dir / shot_name))
                        print(f"  📸 {shot_name}", flush=True)

            for ctx in contexts.values():
                ctx.close()
            browser.close()

        results: dict[str, Path | None] = {}
        for name, wd in webm_dirs.items():
            webms = list(wd.glob("*.webm"))
            if not webms:
                results[name] = None
                continue
            latest = max(webms, key=lambda x: x.stat().st_mtime)
            target = feature_dir / f"{name}.webm"
            latest.rename(target)
            results[name] = ffmpeg_to_mp4(target)
            shutil.rmtree(wd, ignore_errors=True)
        return results
    except Exception as exc:
        print(f"  [WARN] record_simultaneous_perspectives failed: {exc}", flush=True)
        return dict.fromkeys(names)


def _build_grid_html(session_ids: list[str], cols: int, cell_h: int, panel_h: int) -> str:
    """Build the iframe grid HTML for record_fleet_complete."""
    rows = math.ceil(len(session_ids) / cols)
    # Clip exactly 30px from iframe top to hide the hijack toolbar
    _TOOLBAR_PX = 30
    iframes_html = "".join(
        f'<div class="cell"><div class="ifwrap"><iframe id="f{i}" src="/app/session/{sid}"></iframe></div></div>'
        for i, sid in enumerate(session_ids)
    )
    results_cells = "".join(
        f'<div class="rcell" id="rc{i}"><div class="rname">{sid}</div><div class="rout">—</div></div>'
        for i, sid in enumerate(session_ids)
    )
    _ = rows  # used for clarity; grid-template-rows is implicit via auto-fill
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
        "* { margin:0; padding:0; box-sizing:border-box; }"
        " body { background:#0d1117; font-family:monospace; overflow:hidden; }"
        f" .grid {{ display:grid; grid-template-columns:{'1fr ' * cols};"
        f"   height:{cell_h * math.ceil(len(session_ids) / cols)}px; gap:3px; padding:3px 3px 0; }}"
        " .cell { display:flex; flex-direction:column; background:#0d1117;"
        "   border:1px solid #30363d; border-radius:4px; overflow:hidden; }"
        " .ifwrap { flex:1; overflow:hidden; position:relative; }"
        f" .ifwrap iframe {{ position:absolute; top:-{_TOOLBAR_PX}px; left:0;"
        f"   width:100%; height:calc(100% + {_TOOLBAR_PX}px); border:none; }}"
        f" .panel {{ height:{panel_h}px; background:#0d1117; border-top:2px solid #30363d;"
        "   padding:8px; display:flex; flex-direction:column; gap:6px; }}"
        " .panel-title { color:#58a6ff; font-size:10px; font-weight:bold;"
        "   letter-spacing:2px; text-transform:uppercase; flex-shrink:0; }"
        f" .rgroup {{ display:grid; grid-template-columns:{'1fr ' * cols}; gap:6px; flex:1; }}"
        " .rcell { background:#161b22; border:1px solid #30363d; border-radius:4px;"
        "   padding:6px 8px; display:flex; flex-direction:column; gap:3px; overflow:hidden; }"
        " .rname { color:#8b949e; font-size:10px; }"
        " .rout { color:#3fb950; font-size:13px; white-space:pre; overflow:hidden;"
        "   text-overflow:ellipsis; }"
        " .rout.differ { color:#f0883e; }"
        f"</style></head><body>"
        f"<div class='grid'>{iframes_html}</div>"
        "<div class='panel'>"
        "  <div class='panel-title'>Broadcast Results</div>"
        f"  <div class='rgroup' id='rgroup'>{results_cells}</div>"
        "</div></body></html>"
    )


def record_fleet_complete(
    base_url: str,
    session_ids: list[str],
    feature_dir: Path,
    *,
    broadcast_fn: Callable[[Page], None] | None = None,
    settle_s: float = 3.0,
    before_shot: str = "grid-01-before.png",
    after_shot: str = "grid-02-after.png",
) -> dict[str, Path | None]:
    """Open N+1 Playwright contexts simultaneously and record.

    - 1 grid context (FLEET_COLS x rows grid + results bar) -> grid.mp4
    - N worker contexts at /app/session/<sid> → <sid>.mp4 each

    broadcast_fn(grid_page) is called while all N+1 contexts are recording.
    Returns {"grid": Path, "<sid>": Path, ...} — one entry per session plus "grid".
    """
    from playwright.sync_api import sync_playwright

    n = len(session_ids)
    cols = _FLEET_COLS
    rows = math.ceil(n / cols)
    cell_h = 220
    panel_h = 160
    grid_w = 480 * cols
    grid_h = cell_h * rows + panel_h
    worker_w, worker_h = 640, 480

    shots_dir = feature_dir / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    grid_webm_dir = feature_dir / "_webm_grid"
    grid_webm_dir.mkdir(parents=True, exist_ok=True)
    worker_webm_dirs: dict[str, Path] = {}
    for sid in session_ids:
        wd = feature_dir / f"_webm_{sid}"
        wd.mkdir(parents=True, exist_ok=True)
        worker_webm_dirs[sid] = wd

    grid_html = _build_grid_html(session_ids, cols, cell_h, panel_h)
    grid_route = "/__fleet_grid__"
    results: dict[str, Path | None] = {}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            # Browser navigations / page-driven WS carry no bearer header — they
            # authenticate via the uterm_token cookie (dev_token mode). Without
            # this every /app/session/* load 401s and the video records an error.
            auth_cookies = _dev_auth_cookies_or_empty(base_url)

            # Grid context
            grid_ctx = browser.new_context(
                viewport={"width": grid_w, "height": grid_h},
                record_video_dir=str(grid_webm_dir),
                record_video_size={"width": grid_w, "height": grid_h},
            )
            if auth_cookies:
                grid_ctx.add_cookies(auth_cookies)  # type: ignore[arg-type]
            grid_page = grid_ctx.new_page()

            _IFRAME_CSS = (
                "<style>"
                "html,body{margin:0!important;padding:0!important;height:100%!important;overflow:hidden!important}"
                ".page{gap:0!important;padding:0!important;display:flex!important;flex-direction:column!important;height:100vh!important}"
                ".app-header,header{display:none!important}"
                ".card{padding:0!important;margin:0!important;border-radius:0!important}"
                "#widget{min-height:0!important;height:100%!important;flex:1!important}"
                # Widget chrome lives in the <uterm-session> shadow root now, so
                # page CSS can't hide it — the script sets the element's
                # first-class `chromeless` attribute instead. The host is light
                # DOM and still page-stylable, so stretch it to fill the cell.
                "uterm-session{height:100%!important;flex:1!important;min-height:0!important;display:flex!important;flex-direction:column!important}"
                "</style>"
                "<script>(function(){"
                "function hide(el){if(el)el.style.setProperty('display','none','important');}"
                "function applyLayout(){"
                "document.querySelectorAll('uterm-session').forEach(function(el){el.setAttribute('chromeless','');});"
                "hide(document.querySelector('.app-header,header'));"
                "var ss=document.querySelector('#session-status');"
                "if(ss&&ss.closest('.card'))ss.closest('.card').style.setProperty('display','none','important');"
                "var w=document.getElementById('widget');"
                "if(w&&w.closest('.card')){var wc=w.closest('.card');"
                "wc.style.setProperty('flex','1','important');"
                "wc.style.setProperty('overflow','hidden','important');}"
                "}"
                "var obs=new MutationObserver(applyLayout);"
                "obs.observe(document.documentElement,{childList:true,subtree:true});"
                "document.addEventListener('DOMContentLoaded',applyLayout);"
                "})();</script>"
            )

            def _inject_css(route: object, _req: object) -> None:  # type: ignore[misc]
                resp = route.fetch()  # type: ignore[union-attr]
                headers = {
                    k: v
                    for k, v in resp.headers.items()
                    if k.lower() not in ("x-frame-options", "content-security-policy")
                }
                ct = headers.get("content-type", "")
                if "text/html" in ct:
                    html_body = resp.body().decode("utf-8", errors="replace")  # type: ignore[union-attr]
                    # No explicit </head> in this SPA — inject before <body>
                    if "<body>" in html_body:
                        html_body = html_body.replace("<body>", _IFRAME_CSS + "<body>", 1)
                    elif "</head>" in html_body:
                        html_body = html_body.replace("</head>", _IFRAME_CSS + "</head>", 1)
                    route.fulfill(body=html_body.encode("utf-8"), headers=headers, content_type=ct)  # type: ignore[union-attr]
                else:
                    route.fulfill(response=resp, headers=headers)  # type: ignore[union-attr]

            grid_page.route(
                f"**{grid_route}",
                lambda route, _req: route.fulfill(content_type="text/html; charset=utf-8", body=grid_html),
            )
            grid_page.route("**/app/**", _inject_css)
            grid_page.goto(f"{base_url}{grid_route}", wait_until="domcontentloaded")

            # Worker contexts
            worker_ctxs: dict[str, Any] = {}
            worker_pages: dict[str, Any] = {}
            for sid in session_ids:
                wctx = browser.new_context(
                    viewport={"width": worker_w, "height": worker_h},
                    record_video_dir=str(worker_webm_dirs[sid]),
                    record_video_size={"width": worker_w, "height": worker_h},
                )
                if auth_cookies:
                    wctx.add_cookies(auth_cookies)  # type: ignore[arg-type]
                wp = wctx.new_page()
                wp.goto(f"{base_url}/app/session/{sid}", wait_until="domcontentloaded")
                worker_ctxs[sid] = wctx
                worker_pages[sid] = wp

            # Wait for all grid iframes to load
            deadline = time.monotonic() + 25.0
            while time.monotonic() < deadline:
                time.sleep(1.0)
                ready = sum(1 for frame in grid_page.frames[1:] if frame.query_selector(".xterm-viewport") is not None)
                if ready >= n:
                    break

            # Wait for worker pages to load their xterm
            for wp in worker_pages.values():
                with contextlib.suppress(Exception):
                    wp.locator(".xterm-viewport").first.wait_for(state="attached", timeout=15_000)

            time.sleep(1.5)
            grid_page.screenshot(path=str(shots_dir / before_shot))
            print(f"  📸 {before_shot}", flush=True)

            if broadcast_fn is not None:
                broadcast_fn(grid_page)

            time.sleep(settle_s)
            grid_page.screenshot(path=str(shots_dir / after_shot))
            print(f"  📸 {after_shot}", flush=True)

            for wctx in worker_ctxs.values():
                wctx.close()
            grid_ctx.close()
            browser.close()

        # Extract per-worker webms → mp4s (these render correctly)
        for sid in session_ids:
            wd = worker_webm_dirs[sid]
            wwebms = list(wd.glob("*.webm"))
            if wwebms:
                latest = max(wwebms, key=lambda x: x.stat().st_mtime)
                target = feature_dir / f"{sid}.webm"
                latest.rename(target)
                results[sid] = ffmpeg_to_mp4(target)
            else:
                results[sid] = None

        # Build the grid composite by tiling the per-worker videos (which
        # render) and appending the broadcast-results panel cropped from the
        # grid screenshot. The live iframe grid records blank: /app/session
        # cannot be framed (X-Frame-Options: deny) and, once that is stripped,
        # the in-iframe terminal widget still never mounts.
        results["grid"] = tile_grid_with_footer(
            [results.get(sid) for sid in session_ids],
            shots_dir / after_shot,
            feature_dir / "grid.mp4",
            cols=cols,
            cell_w=480,
            cell_h=cell_h,
            footer_h=panel_h,
        )

    except Exception as exc:
        print(f"  [WARN] record_fleet_complete failed: {exc}", flush=True)
        for key in ["grid", *session_ids]:
            results.setdefault(key, None)

    finally:
        shutil.rmtree(grid_webm_dir, ignore_errors=True)
        for wd in worker_webm_dirs.values():
            shutil.rmtree(wd, ignore_errors=True)

    return results
