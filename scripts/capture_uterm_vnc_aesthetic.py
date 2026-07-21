#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Capture aesthetic/integration proof of the first-party uterm VNC console.

Starts lab + server (via prove_uterm_vnc_console helpers), opens the polished
``vnc.html`` page, and writes:

* full-page screenshot (product chrome + remote desktop)
* canvas crop (desktop content only)
* short webm of the connected console
* status/dims JSON for programmatic checks

Usage::

    uv run python scripts/capture_uterm_vnc_aesthetic.py
    uv run python scripts/capture_uterm_vnc_aesthetic.py --evidence-dir ./demo/vnc-lab/screenshots
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import prove_uterm_vnc_console as prove  # noqa: E402
import prove_vnc_lab as vnc_lab  # noqa: E402

LAB = "uterm-vnc-aesthetic"
SESSION = prove.SERVER_SESSION
TARGET = prove.TARGET_PLAIN


def _evidence_dir(explicit: str | None) -> Path:
    for key in (explicit, os.environ.get("EVIDENCE_DIR"), os.environ.get("SCRATCH")):
        if key:
            p = Path(key)
            p.mkdir(parents=True, exist_ok=True)
            return p
    p = Path("demo/vnc-lab/screenshots")
    p.mkdir(parents=True, exist_ok=True)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", default=None)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--hold-s", type=float, default=4.0, help="seconds to hold for video")
    args = parser.parse_args(argv)

    evidence = _evidence_dir(args.evidence_dir)
    root = prove._repo_root()
    lines: list[str] = [f"evidence={evidence}"]

    if not vnc_lab._docker_available():
        (evidence / "docker-unavailable.log").write_text("docker missing\n", encoding="utf-8")
        return 2

    if not args.skip_build:
        vnc_lab.build_image(root=root, log_path=evidence / "aesthetic-lab-build.log")
    plain, tls = vnc_lab.start_container(name=LAB, demo_url="https://example.com")
    vnc_lab.wait_rfb("127.0.0.1", plain, retries=60, delay=0.5)
    lines.append(f"lab_plain={plain}")

    host, port = "127.0.0.1", prove._free_port()
    cfg = evidence / "aesthetic-server.toml"
    lab_worker_token = "vnc-lab-worker-bearer-token-32chars"  # noqa: S105 — lab fixture only
    prove.write_server_config(
        cfg,
        host=host,
        port=port,
        plain_port=plain,
        tls_port=tls,
        token=lab_worker_token,
    )
    proc = prove.start_server(root=root, config=cfg, log_path=evidence / "aesthetic-server.log")
    base = f"http://{host}:{port}"
    try:
        prove.wait_http(f"{base}/readyz", timeout=45.0)
        hijack_id = prove.wait_worker_and_acquire(base)
        lines.append(f"hijack_id={hijack_id}")
        page_url = f"{base}/_terminal/vnc.html?worker_id={SESSION}&hijack_id={hijack_id}&target_id={TARGET}"
        lines.append(f"page_url={page_url}")

        from playwright.sync_api import sync_playwright

        full = evidence / "uterm-vnc-aesthetic-full.png"
        crop = evidence / "uterm-vnc-aesthetic-desktop.png"
        video_dir = evidence / "video-raw"
        video_dir.mkdir(exist_ok=True)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                device_scale_factor=2,
                record_video_dir=str(video_dir),
                record_video_size={"width": 1440, "height": 900},
                extra_http_headers={
                    "x-uterm-principal": "alice",
                    "x-uterm-role": "admin",
                    "x-uterm-tenant": "lab",
                },
            )
            context.add_cookies(
                [
                    {"name": "uterm_principal", "value": "alice", "domain": host, "path": "/"},
                    {"name": "uterm_role", "value": "admin", "domain": host, "path": "/"},
                    {"name": "uterm_tenant", "value": "lab", "domain": host, "path": "/"},
                ]
            )
            page = context.new_page()
            page.goto(page_url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_function(
                """() => {
                  const el = document.getElementById('vnc-status');
                  return el && el.dataset.state === 'connected';
                }""",
                timeout=20_000,
            )
            # Allow framebuffer paint + chrome dims refresh.
            page.wait_for_timeout(2800)

            # Assert product chrome markers (integration, not bare noVNC).
            chrome = page.evaluate(
                """() => {
                  const status = document.getElementById('vnc-status');
                  const eyebrow = document.querySelector('.eyebrow');
                  const brand = document.querySelector('.brand-mark');
                  const chromeTitle = document.querySelector('.viewport-chrome .title');
                  const dims = document.getElementById('vnc-dims');
                  const canvas = document.querySelector('#vnc-screen canvas');
                  const primary = document.querySelector('#vnc-connect.primary');
                  return {
                    status_state: status?.dataset?.state ?? null,
                    status_text: status?.textContent ?? null,
                    eyebrow: eyebrow?.textContent?.trim() ?? null,
                    has_brand_mark: !!brand,
                    chrome_title: chromeTitle?.textContent?.trim() ?? null,
                    dims: dims?.textContent?.trim() ?? null,
                    canvas_w: canvas?.width ?? 0,
                    canvas_h: canvas?.height ?? 0,
                    has_primary_cta: !!primary,
                    title: document.title,
                  };
                }"""
            )
            (evidence / "aesthetic-metrics.json").write_text(json.dumps(chrome, indent=2) + "\n", encoding="utf-8")
            assert chrome["status_state"] == "connected", chrome
            assert chrome["eyebrow"] == "provide-uterm", chrome
            assert chrome["has_brand_mark"] is True, chrome
            assert chrome["chrome_title"] == "Remote desktop", chrome
            assert chrome["has_primary_cta"] is True, chrome
            assert chrome["canvas_w"] >= 640 and chrome["canvas_h"] >= 480, chrome
            assert "provide-uterm" in (chrome["title"] or "").lower()

            page.screenshot(path=str(full), full_page=True)
            # Crop the remote desktop panel (main widget) for content proof.
            screen = page.locator("#vnc-screen")
            screen.screenshot(path=str(crop))

            # Hold for video of the live desktop.
            page.wait_for_timeout(int(args.hold_s * 1000))
            context.close()
            browser.close()

        # Move playwright video to a stable name.
        webms = list(video_dir.glob("*.webm"))
        video_out = evidence / "uterm-vnc-aesthetic.webm"
        if webms:
            webms[0].replace(video_out)
            lines.append(f"video={video_out}")
        else:
            lines.append("video=missing")

        # Also publish into demo/screenshots when evidence is elsewhere.
        demo = root / "demo" / "vnc-lab" / "screenshots"
        demo.mkdir(parents=True, exist_ok=True)
        for src, name in (
            (full, "uterm-vnc-console.png"),
            (full, "uterm-vnc-aesthetic-full.png"),
            (crop, "uterm-vnc-aesthetic-desktop.png"),
        ):
            if src.is_file():
                dest = demo / name
                dest.write_bytes(src.read_bytes())
                lines.append(f"demo_copy={dest}")

        if video_out.is_file():
            demo_v = root / "demo" / "vnc-lab" / "uterm-vnc-aesthetic.webm"
            # webm may be gitignored under demo/**/*.webm — still write for local proof
            demo_v.write_bytes(video_out.read_bytes())
            lines.append(f"demo_video={demo_v}")

        summary = "\n".join(lines) + "\naesthetic_proof=ok\n"
        (evidence / "aesthetic-summary.log").write_text(summary, encoding="utf-8")
        print(summary)
        print(json.dumps(chrome, indent=2))
        return 0
    except Exception as exc:
        err = "\n".join(lines) + f"\nerror={exc}\n"
        (evidence / "aesthetic-error.log").write_text(err, encoding="utf-8")
        print(err, file=sys.stderr)
        return 1
    finally:
        prove.stop_server(proc)
        vnc_lab._remove_container(LAB)
        time.sleep(0.2)


if __name__ == "__main__":
    raise SystemExit(main())
