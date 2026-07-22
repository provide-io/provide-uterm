#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Record the recursive "nautilus" tiling panels page.

Opens the first-party ``panels.html`` (VNC + terminal split screen), then splits
the layout a few times into a golden-ratio spiral of live panes. VNC panes view
lab RFB targets through the human-VNC relay; terminal panes attach to the shell
session directly. Proves the tiling UI live and captures a screenshot + mp4.

Usage (repo root)::

    uv run python scripts/record_uterm_vnc_panels_demo.py --skip-build
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import prove_vnc_lab as vnc_lab  # noqa: E402
import record_uterm_vnc_demo_video as base  # noqa: E402
import record_uterm_vnc_nested_demo as nested  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", default=None)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--splits", type=int, default=5, help="how many times to spiral-split (nautilus)")
    args = parser.parse_args(argv)

    evidence = base._evidence_dir(args.evidence_dir)
    root = base._repo_root()
    shots = evidence / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [f"evidence={evidence}"]

    if not vnc_lab._docker_available():
        (evidence / "docker-unavailable.log").write_text("docker missing\n", encoding="utf-8")
        return 2
    if not args.skip_build:
        vnc_lab.build_image(root=root, log_path=evidence / "lab-build.log")

    server_port = base._free_port()
    cfg = evidence / "panels-server.toml"
    # Reuse the nested config: shell terminal + N VNC-lease workers + N RFB targets.
    nested._write_nested_config(cfg, host="0.0.0.0", port=server_port)

    env = {**os.environ, "UTERM_TEST_MODE": "1", "UTERM_API_ONLY": "0"}
    log_f = (evidence / "panels-server.log").open("w", encoding="utf-8")
    cmd = [str(root / ".venv" / "bin" / "uterm"), "server", "--config", str(cfg)]
    if not Path(cmd[0]).is_file():
        cmd = [sys.executable, "-m", "provide.uterm.cli", "server", "--config", str(cfg)]
    proc = subprocess.Popen(cmd, cwd=str(root), stdout=log_f, stderr=subprocess.STDOUT, text=True, env=env)

    headers = {
        "x-uterm-principal": "test-admin",
        "x-uterm-role": "admin",
        "x-uterm-tenant": "lab",
        "Content-Type": "application/json",
    }
    base_url = f"http://127.0.0.1:{server_port}"
    metrics: dict[str, object] = {}
    lab_names = nested.NEST_NAMES
    try:
        base.prove.wait_http(f"{base_url}/readyz", timeout=60.0)
        lines.append(f"server_ready={base_url}")

        connected = False
        for _ in range(80):
            st, body = base.http_json("GET", f"{base_url}/api/sessions/{base.SHELL_SESSION}", headers=headers)
            if st == 200 and isinstance(body, dict) and body.get("connected"):
                connected = True
                break
            time.sleep(0.15)
        if not connected:
            raise RuntimeError("shell session never connected")
        shell_hid, _ = base.acquire_shell(base_url, headers, session=base.SHELL_SESSION)
        if not shell_hid:
            raise RuntimeError("failed to acquire shell lease")
        for cmd_line in nested.SEED_LINES:
            base.send_shell_keys(base_url, headers, shell_hid, cmd_line)
            time.sleep(0.05)

        # Start each lab on the text terminal so its RFB target shows real content.
        hids = [nested._acquire_lease(base_url, headers, w) for w in nested.NEST_LEASE_WORKERS]
        term_url = (
            f"http://{base._HOST_FROM_DOCKER}:{server_port}"
            f"/_terminal/terminal.html?worker_id={base.SHELL_SESSION}&role=browser"
        )
        for name, port in zip(lab_names, nested.NEST_PORTS, strict=True):
            nested._start_nested_lab(name=name, demo_url=term_url, host_plain=port)
            if not nested._wait_lab_rfb(name):
                raise RuntimeError(f"lab {name} RFB never ready")
        time.sleep(3.0)

        # Panels URL: VNC pool = the lab targets (fan-out), terminal pool = shell.
        vnc_pool = ",".join(
            f"{w}~{h}~{t}" for w, h, t in zip(nested.NEST_LEASE_WORKERS, hids, nested.NEST_TARGETS, strict=True)
        )
        # No `term` pool: every pane (including the terminal leaves) renders as a
        # VNC-of-terminal, so all panes show the live colour/cat demo and shrink
        # via noVNC's scale-viewport as the nautilus spirals inward.
        panels_url = f"{base_url}/_terminal/panels.html?vnc={vnc_pool}"
        lines.append(f"panels_url={panels_url}")

        from playwright.sync_api import sync_playwright

        raw_dir = evidence / "panels-video-raw"
        raw_dir.mkdir(exist_ok=True)
        full_png = shots / "uterm-vnc-fractal-full.png"
        live_webm: Path | None = None
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1600, "height": 960},
                device_scale_factor=1,
                record_video_dir=str(raw_dir),
                record_video_size={"width": 1600, "height": 960},
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
                {"x-uterm-principal": "test-admin", "x-uterm-role": "admin", "x-uterm-tenant": "lab"}
            )
            page.goto(panels_url, wait_until="domcontentloaded", timeout=45_000)
            # Initial VNC+terminal split renders two panes.
            page.wait_for_function("() => (window.utermPanels?.leafCount ?? 0) >= 2", timeout=30_000)
            # At least one VNC pane connects through the relay.
            page.wait_for_function(
                "() => document.querySelector('.pane-vnc[data-state=\"connected\"]') !== null",
                timeout=40_000,
            )
            page.wait_for_timeout(2500)
            # Spiral fresh VNC+terminal units inward — each click shrinks the
            # existing layout into the golden-minor corner (the nautilus fractal).
            for _ in range(max(0, int(args.splits))):
                page.click("#panels-nautilus")
                page.wait_for_timeout(3000)
            page.wait_for_timeout(4000)
            page.screenshot(path=str(full_png), full_page=True)
            metrics.update(
                page.evaluate(
                    "() => ({ panes: window.utermPanels?.leafCount ?? 0,"
                    " connectedVnc: document.querySelectorAll('.pane-vnc[data-state=\"connected\"]').length,"
                    " termPanes: document.querySelectorAll('.pane-term').length }) "
                )
            )
            video = page.video
            context.close()
            browser.close()
            if video is not None:
                try:
                    live_webm = Path(video.path())
                except Exception:  # pragma: no cover - best-effort
                    live_webm = None

        lines.append(f"metrics={json.dumps(metrics)}")
        if int(metrics.get("panes", 0) or 0) < 2:
            raise RuntimeError(f"panels never rendered: {metrics}")

        video_out = evidence / "uterm-vnc-fractal.mp4"
        if live_webm and live_webm.is_file():
            nested._encode_mp4(live_webm, video_out)
            base._assert_video_not_black(video_out)
            lines.append(f"video={video_out}")

        demo = root / "demo" / "vnc-lab"
        (demo / "screenshots").mkdir(parents=True, exist_ok=True)
        (demo / "screenshots" / "uterm-vnc-fractal-full.png").write_bytes(full_png.read_bytes())
        if video_out.is_file():
            (demo / "uterm-vnc-fractal.mp4").write_bytes(video_out.read_bytes())
        (evidence / "panels-summary.log").write_text("\n".join(lines) + "\npanels_proof=ok\n", encoding="utf-8")
        print("\n".join(lines))
        return 0
    except Exception as exc:
        lines.append(f"error={exc}")
        (evidence / "panels-summary.log").write_text("\n".join(lines) + "\npanels_proof=FAIL\n", encoding="utf-8")
        print("\n".join(lines), file=sys.stderr)
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        for name in lab_names:
            vnc_lab._remove_container(name)


if __name__ == "__main__":
    raise SystemExit(main())
