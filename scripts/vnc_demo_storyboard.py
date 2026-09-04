#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Playwright storyboard capture for the VNC console demo.

Extracted from ``record_uterm_vnc_demo_video.py`` to keep that script under the
777-line cap. This is the browser half only: open the console page once per
chapter, fire the chapter's keystrokes, and grab a clean connected frame.

The chapter driver and the desktop nudge arrive as callables rather than
imports. That keeps the dependency pointing one way — the recorder owns the
shell/HTTP helpers its three sibling demo scripts import from it, and this
module never imports back into it.
"""

from __future__ import annotations

import subprocess  # nosec B404 - fixed argv, no shell
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path


def nudge_lab_desktop(lab_name: str) -> None:
    """Force X damage on the lab Chromium window without tearing the RFB session.

    Chromium canvas under Xvfb often skips incremental damage; a 1px resize
    makes x11vnc push a full frame so noVNC updates without connect/disconnect
    flashing.
    """
    script = r"""
set -e
W=1280; H=720
for pat in Chromium chromium Chrome chrome; do
  ids=$(xdotool search --onlyvisible --class "$pat" 2>/dev/null || true)
  for id in $ids; do
    xdotool windowsize "$id" $((W-1)) "$H" 2>/dev/null || true
    xdotool windowsize "$id" "$W" "$H" 2>/dev/null || true
    xdotool windowmove "$id" 0 0 2>/dev/null || true
    exit 0
  done
done
exit 0
"""
    try:
        subprocess.run(
            ["docker", "exec", lab_name, "bash", "-lc", script],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def record_storyboard(
    *,
    evidence: Path,
    shots: Path,
    base: str,
    headers: dict[str, str],
    shell_hid: str,
    demo_url: str,
    page_url: str,
    lines: list[str],
    tail_seconds: float,
    chapters: Sequence[Sequence[str]],
    key_gap_s: float,
    lab_name: str,
    send_shell_keys: Callable[..., tuple[int, Any]],
) -> tuple[list[Path], dict[str, object], Path, Path, Path, Path]:
    """Drive the chapters and capture one frame each.

    Returns ``(frame_paths, metrics, storyboard_dir, metrics_path, full_png,
    desktop_png)``. The caller asserts on the metrics and stitches the frames —
    validation stays where the failure is reported.
    """
    LIVE_DEMO_CHAPTERS = chapters
    _KEY_GAP_S = key_gap_s
    LAB = lab_name

    class _Args:
        seconds = tail_seconds

    args = _Args()

    from playwright.sync_api import sync_playwright

    full_png = shots / "uterm-vnc-text-demos-full.png"
    desktop_png = shots / "uterm-vnc-text-demos-desktop.png"
    metrics_path = evidence / "video-metrics.json"
    storyboard = evidence / "storyboard"
    storyboard.mkdir(exist_ok=True)
    frame_paths: list[Path] = []
    metrics: dict[str, object] = {
        "demo_url": demo_url,
        "vnc_page": page_url,
    }

    def _open_vnc_page(browser: object) -> tuple[object, object]:
        """Open a clean VNC console page (one connect, no mid-stream reconnect)."""
        context = browser.new_context(  # type: ignore[attr-defined]
            viewport={"width": 1440, "height": 900},
            device_scale_factor=1,
        )
        context.add_cookies(
            [
                {"name": "uterm_principal", "value": "test-admin", "domain": "127.0.0.1", "path": "/"},
                {"name": "uterm_role", "value": "admin", "domain": "127.0.0.1", "path": "/"},
                {"name": "uterm_tenant", "value": "lab", "domain": "127.0.0.1", "path": "/"},
            ]
        )
        page = context.new_page()
        page.set_extra_http_headers(
            {
                "x-uterm-principal": "test-admin",
                "x-uterm-role": "admin",
                "x-uterm-tenant": "lab",
            }
        )
        page.goto(page_url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_function(
            """() => {
              const el = document.getElementById('vnc-status');
              return el && el.dataset.state === 'connected';
            }""",
            timeout=30_000,
        )
        page.wait_for_function(
            """() => {
              const c = document.querySelector('#vnc-screen canvas');
              return c instanceof HTMLCanvasElement && c.width >= 640 && c.height >= 400;
            }""",
            timeout=20_000,
        )
        # Let the first full framebuffer settle (readable glyphs, no flash).
        page.wait_for_timeout(700)
        return context, page

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # Progressive storyboard: fire a rapid chapter, then capture a *clean*
        # connected VNC frame. No mid-recording RFB disconnect (that flashed
        # Connecting… and shredded the demo). ffmpeg stitches frames into a
        # snappy website-style walkthrough.
        for chap_i, chapter in enumerate(LIVE_DEMO_CHAPTERS, start=1):
            for cmd in chapter:
                st_send, body_send = send_shell_keys(base, headers, shell_hid, cmd)
                if st_send >= 400:
                    lines.append(f"chap{chap_i}_err={st_send} {body_send!r}")
                time.sleep(_KEY_GAP_S)
            # Nested browser paints via WS; nudge X so the lab desktop updates.
            time.sleep(0.35)
            nudge_lab_desktop(LAB)
            time.sleep(0.55)

            ctx, page = _open_vnc_page(browser)
            frame_path = storyboard / f"chap_{chap_i:02d}.png"
            page.screenshot(path=str(frame_path), full_page=True)
            frame_paths.append(frame_path)
            if chap_i == len(LIVE_DEMO_CHAPTERS):
                page.screenshot(path=str(full_png), full_page=True)
                page.locator("#vnc-screen").screenshot(path=str(desktop_png))
                metrics.update(
                    page.evaluate(
                        """() => {
                          const status = document.getElementById('vnc-status');
                          const canvas = document.querySelector('#vnc-screen canvas');
                          const dims = document.getElementById('vnc-dims');
                          const screen = document.getElementById('vnc-screen');
                          const cs = screen ? getComputedStyle(screen) : null;
                          const sr = screen ? screen.getBoundingClientRect() : null;
                          const cr = canvas ? canvas.getBoundingClientRect() : null;
                          return {
                            status_state: status?.dataset?.state ?? null,
                            status_text: status?.textContent ?? null,
                            dims: dims?.textContent ?? null,
                            canvas_w: canvas?.width ?? 0,
                            canvas_h: canvas?.height ?? 0,
                            title: document.title,
                            screen_pad: cs?.padding ?? null,
                            screen_radius: cs?.borderRadius ?? null,
                            screen_box: sr ? [Math.round(sr.width), Math.round(sr.height)] : null,
                            canvas_box: cr ? [Math.round(cr.width), Math.round(cr.height)] : null,
                          };
                        }"""
                    )
                )
            lines.append(f"chapter_{chap_i}_frame={frame_path.name}")
            ctx.close()

        # Optional short continuous clip of the final state (stable RFB, no reconnect).
        tail_s = max(0.0, float(args.seconds))
        if tail_s > 0.2:
            video_dir = evidence / "video-raw"
            video_dir.mkdir(exist_ok=True)
            ctx = browser.new_context(
                viewport={"width": 1440, "height": 900},
                device_scale_factor=1,
                record_video_dir=str(video_dir),
                record_video_size={"width": 1440, "height": 900},
            )
            ctx.add_cookies(
                [
                    {"name": "uterm_principal", "value": "test-admin", "domain": "127.0.0.1", "path": "/"},
                    {"name": "uterm_role", "value": "admin", "domain": "127.0.0.1", "path": "/"},
                    {"name": "uterm_tenant", "value": "lab", "domain": "127.0.0.1", "path": "/"},
                ]
            )
            page = ctx.new_page()
            page.set_extra_http_headers(
                {
                    "x-uterm-principal": "test-admin",
                    "x-uterm-role": "admin",
                    "x-uterm-tenant": "lab",
                }
            )
            page.goto(page_url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_function(
                """() => document.getElementById('vnc-status')?.dataset?.state === 'connected'""",
                timeout=30_000,
            )
            page.wait_for_timeout(int(tail_s * 1000))
            ctx.close()
            lines.append(f"tail_clip_s={tail_s}")

        browser.close()

    return frame_paths, metrics, storyboard, metrics_path, full_png, desktop_png
