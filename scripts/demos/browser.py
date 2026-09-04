#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Single-perspective browser recording helpers for demo recording scripts."""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from scripts.demos.ffmpeg import ffmpeg_to_mp4


def _attach_console_debug(page: object) -> None:
    """When DEMO_DEBUG_CONSOLE is set, echo page console + uncaught errors.

    Demo widgets mount inside iframes/custom pages where a silent JS failure
    (bad import, throwing handler) just yields a blank video. This surfaces them.
    """
    if not os.environ.get("DEMO_DEBUG_CONSOLE"):
        return
    page.on("console", lambda m: print(f"  [console.{m.type}] {m.text}", flush=True))  # type: ignore[union-attr]
    page.on("pageerror", lambda e: print(f"  [pageerror] {e}", flush=True))  # type: ignore[union-attr]


if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Browser, BrowserContext, Page

    from scripts.demos.server import BrowserStep


def wait_for_terminal(page: Page, timeout: float = 15.0) -> bool:
    """Wait for xterm.js to render content into the DOM. Returns True on success."""
    try:
        # Playwright locators pierce the <uterm-session> open shadow root; the
        # terminal renders `.xterm-rows` whether the page mounts the session
        # widget (shadow DOM) or the bare <uterm-terminal> (light DOM).
        page.locator(".xterm-rows span").first.wait_for(state="attached", timeout=timeout * 1000)
        return True
    except Exception:
        return False


def wait_for_presence_bar(page: Page, min_users: int = 2, timeout: float = 10.0) -> bool:
    """Wait for DeckMux presence bar with at least min_users avatars. Returns True/False."""
    try:
        # Avatars live inside the <uterm-presence-bar> shadow root, which a plain
        # document.querySelectorAll can't see; a Playwright locator pierces it.
        # Waiting for the (min_users-1)th node asserts the count is reached.
        page.locator(".dm-avatar").nth(min_users - 1).wait_for(state="attached", timeout=timeout * 1000)
        return True
    except Exception:
        return False


def type_in_terminal(page: Page, text: str) -> None:
    """Send input to the terminal via the hijack input row."""
    field = page.locator("#inputfield")
    field.fill(text)
    page.locator("#inputsend").click()


def wait_for_status(page: Page, text: str, timeout: float = 10.0) -> bool:
    """Wait for the status text element to contain `text`. Returns True/False."""
    try:
        page.locator("#statustext", has_text=text).wait_for(state="visible", timeout=timeout * 1000)
        return True
    except Exception:
        return False


def click_hijack(page: Page, timeout: float = 10.0) -> bool:
    """Wait for the hijack button and click it. Returns True/False."""
    btn = page.locator("#hijack")
    try:
        btn.wait_for(state="visible", timeout=timeout * 1000)
        # Playwright's click() runs actionability checks (including waiting for
        # the button to become enabled), so the old getElementById disabled-poll
        # — which couldn't see into the shadow root anyway — is unnecessary.
        btn.click(timeout=timeout * 1000)
        return True
    except Exception:
        return False


def open_background_context(
    browser: Browser,
    base_url: str,
    path: str,
    wait_s: float = 2.0,
) -> tuple[BrowserContext, Page]:
    """Open a second browser context (no recording) and navigate to base_url+path.

    Returns (context, page) — caller must close context when done.
    """
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 720},
        extra_http_headers=_dev_auth_headers_or_empty(),
    )
    page = ctx.new_page()
    full = base_url + path if path.startswith("/") else path
    page.goto(full)
    page.wait_for_load_state("networkidle")
    time.sleep(wait_s)
    return ctx, page


def _run_steps(page: Page, steps: list[BrowserStep], shots_dir: Path) -> None:
    """Execute a list of BrowserStep against a Playwright page."""
    for action, wait_s, shot_name in steps:
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


def _dev_auth_headers_or_empty() -> dict[str, str]:
    """Pull the dev_token bearer header if start_server() set one up, else empty.

    Browser-side ``/app/`` pages now require a JWT after dab4ac2 removed
    the dev/none auth bypass. Without this header on every page load,
    playwright captures a 401 page instead of the demo UI.
    """
    try:
        from scripts.demos.server import dev_bearer_headers

        return dev_bearer_headers()
    except Exception:
        return {}


def _dev_auth_cookies_or_empty(base_url: str) -> list[dict[str, object]]:
    """Build a list of cookies that authenticate page-driven fetches/WebSockets.

    ``_dev_auth_headers_or_empty`` only covers navigation requests. WebSocket
    connections opened from inside the page (e.g. the <uterm-session> element
    constructing ``new WebSocket("/ws/browser/...")``) don't carry custom
    HTTP headers — browsers don't allow it. They *do* carry cookies. The
    server's auth resolver reads the ``uterm_token`` cookie via the same
    JWT validation codepath, so dropping the bearer into that cookie gives
    the WS handshake a credential it can verify.
    """
    headers = _dev_auth_headers_or_empty()
    bearer = headers.get("Authorization", "")
    token = bearer.removeprefix("Bearer ").strip() if bearer.startswith("Bearer ") else ""
    if not token:
        return []
    # Playwright requires either ``url`` *or* ``domain`` — not both. Use the
    # url form so domain + path + secure are inferred from base_url.
    return [
        {
            "name": "uterm_token",
            "value": token,
            "url": base_url,
        }
    ]


def record_perspective(
    name: str,
    base_url: str,
    steps: list[BrowserStep],
    feature_dir: Path,
    cookies: list[dict[str, str]] | None = None,
) -> Path | None:
    """Record a single named browser perspective. Returns mp4 path or None.

    *cookies* authenticate a server running ``auth.mode = "header"``. A browser
    WebSocket cannot carry custom headers, so a page that opens one — the human
    VNC relay, for instance — is refused 401 on header auth unless the principal
    arrives in a cookie. Without this the recording is of a "Connection lost"
    panel, which is how it was found.
    """
    from playwright.sync_api import sync_playwright

    shots_dir = feature_dir / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    resolved: list[BrowserStep] = [
        (base_url + action if isinstance(action, str) and action.startswith("/") else action, w, s)
        for action, w, s in steps
    ]
    try:
        with sync_playwright() as p:
            # Disable Chromium's Local/Private Network Access checks: demos whose
            # page is served via route.fulfill() get an "unknown" address space,
            # so a WebSocket to the localhost server is treated as a public→local
            # request and blocked (net::ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS).
            # This is a headless recorder, so relaxing the check is safe.
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessChecks"],
            )
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 720},
                record_video_dir=str(feature_dir),
                record_video_size={"width": 1280, "height": 720},
                extra_http_headers=_dev_auth_headers_or_empty(),
            )
            if cookies:
                ctx.add_cookies(cookies)  # type: ignore[arg-type]
            _auth_cookies = _dev_auth_cookies_or_empty(base_url)
            if _auth_cookies:
                ctx.add_cookies(_auth_cookies)  # type: ignore[arg-type]
            page = ctx.new_page()
            _attach_console_debug(page)
            _run_steps(page, resolved, shots_dir)
            ctx.close()
            browser.close()
        webms = list(feature_dir.glob("*.webm"))
        if not webms:
            return None
        latest = max(webms, key=lambda x: x.stat().st_mtime)
        target = feature_dir / f"{name}.webm"
        latest.rename(target)
        return ffmpeg_to_mp4(target)
    except Exception as exc:
        print(f"  [WARN] record_perspective({name}) failed: {exc}", flush=True)
        return None


def record_perspective_with_background(
    name: str,
    base_url: str,
    steps: list[BrowserStep],
    feature_dir: Path,
    background_path: str,
    background_wait_s: float = 2.0,
) -> Path | None:
    """Record a perspective while a background (non-recorded) context stays open."""
    from playwright.sync_api import sync_playwright

    shots_dir = feature_dir / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    resolved: list[BrowserStep] = [
        (base_url + action if isinstance(action, str) and action.startswith("/") else action, w, s)
        for action, w, s in steps
    ]
    try:
        with sync_playwright() as p:
            # Disable Chromium's Local/Private Network Access checks: demos whose
            # page is served via route.fulfill() get an "unknown" address space,
            # so a WebSocket to the localhost server is treated as a public→local
            # request and blocked (net::ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS).
            # This is a headless recorder, so relaxing the check is safe.
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessChecks"],
            )
            bg_ctx, _bg_page = open_background_context(browser, base_url, background_path, wait_s=background_wait_s)
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 720},
                record_video_dir=str(feature_dir),
                record_video_size={"width": 1280, "height": 720},
                extra_http_headers=_dev_auth_headers_or_empty(),
            )
            _auth_cookies = _dev_auth_cookies_or_empty(base_url)
            if _auth_cookies:
                ctx.add_cookies(_auth_cookies)  # type: ignore[arg-type]
            page = ctx.new_page()
            _attach_console_debug(page)
            _run_steps(page, resolved, shots_dir)
            ctx.close()
            bg_ctx.close()
            browser.close()
        webms = list(feature_dir.glob("*.webm"))
        if not webms:
            return None
        latest = max(webms, key=lambda x: x.stat().st_mtime)
        target = feature_dir / f"{name}.webm"
        latest.rename(target)
        return ffmpeg_to_mp4(target)
    except Exception as exc:
        print(f"  [WARN] record_perspective_with_background({name}) failed: {exc}", flush=True)
        return None


def browser_record(
    base_url: str,
    steps: list[BrowserStep],
    feature_dir: Path,
    cookies: list[dict[str, str]] | None = None,
) -> Path | None:
    """Record a browser session (single perspective, named 'browser')."""
    return record_perspective("browser", base_url, steps, feature_dir, cookies)


def browser_record_multi(
    base_url: str,
    perspectives: dict[str, list[BrowserStep]],
    feature_dir: Path,
) -> dict[str, Path | None]:
    """Record multiple named browser perspectives sequentially."""
    return {name: record_perspective(name, base_url, steps, feature_dir) for name, steps in perspectives.items()}
