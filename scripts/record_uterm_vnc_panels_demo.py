#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Record the recursive "nautilus" tiling panels page with distinct live scenes.

Each of the three lab RFB targets natively plays a *different* animated scene (a
rainbow gradient, a blinking cat, matrix rain) via mpv, which blits whole decoded
frames to Xvfb — so x11vnc streams clean, tear-free motion to the already
connected noVNC pane. (A nested Chromium never presents animation frames to Xvfb
at all, and a software terminal tears mid-draw; mpv avoids both.) The nautilus
panes fan out across the three targets, so the tiling shows multiple *distinct*
VNC feeds that actually animate. Panes shrink as the spiral winds inward via
noVNC's scale-viewport.

Usage (repo root)::

    uv run python scripts/record_uterm_vnc_panels_demo.py --skip-build
"""

from __future__ import annotations

import argparse
import colorsys
import json
import os
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import prove_vnc_lab as vnc_lab  # noqa: E402
import record_uterm_vnc_demo_video as base  # noqa: E402
import record_uterm_vnc_nested_demo as nested  # noqa: E402

# (id, display label, scene GIF filename). One per lab target so each VNC pane
# shows a visibly different *animated* scene; mpv loops the GIF natively in the
# lab, so the panes actually play.
SCENES = [
    ("scene-rainbow", "Rainbow", "rainbow.gif"),
    ("scene-cat", "Cat", "cat.gif"),
    ("scene-matrix", "Matrix", "matrix.gif"),
]

# Animated-GIF geometry. mpv decodes the GIF and blits whole frames to X, so the
# per-frame duration (≈14 fps) drives smooth, tear-free playback; mpv scales the
# clip up to the lab framebuffer.
_SCENE_W, _SCENE_H = 320, 180
_SCENE_FRAMES = 16
_SCENE_DURATION_MS = 70


def _save_gif(frames: list[Any], path: Path) -> None:
    """Write *frames* as a looping animated GIF at the shared scene cadence."""
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=_SCENE_DURATION_MS,
        loop=0,
        disposal=2,
    )


def _rainbow_frames() -> list[Any]:
    """Hue sweep that scrolls one full cycle across the animation (looping)."""
    from PIL import Image

    w, h, n = _SCENE_W, _SCENE_H, _SCENE_FRAMES
    out: list[Any] = []
    for f in range(n):
        img = Image.new("RGB", (w, h))
        px = img.load()
        phase = f / n
        for x in range(w):
            r, g, b = colorsys.hsv_to_rgb((x / w + phase) % 1.0, 1.0, 1.0)
            col = (int(r * 255), int(g * 255), int(b * 255))
            for y in range(h):
                px[x, y] = col
        out.append(img)
    return out


def _cat_frames() -> list[Any]:
    """Cat face with green eyes: pupils slide side to side, with a mid-loop blink.

    Every frame differs (moving pupils) so PIL keeps all 16 — smooth motion, not
    a collapsed near-static GIF.
    """
    import math

    from PIL import Image

    w, h, n = _SCENE_W, _SCENE_H, _SCENE_FRAMES
    cx, base_cy = w // 2, h // 2 + 8
    out: list[Any] = []
    for f in range(n):
        img = Image.new("RGB", (w, h), (18, 18, 28))
        px = img.load()
        cy = base_cy + round(7 * math.sin(2 * math.pi * f / n))  # whole head bobs
        for x in range(w):
            for y in range(h):
                if (x - cx) ** 2 + ((y - cy) * 1.2) ** 2 < 60**2:
                    px[x, y] = (95, 72, 52)
        for x in range(cx - 55, cx - 15):
            for y in range(cy - 70, cy - 30):
                if (x - (cx - 55)) < (y - (cy - 70)) < (x - (cx - 55)) + 18:
                    px[x, y] = (95, 72, 52)
        for x in range(cx + 15, cx + 55):
            for y in range(cy - 70, cy - 30):
                if ((cx + 55) - x) < (y - (cy - 70)) < ((cx + 55) - x) + 18:
                    px[x, y] = (95, 72, 52)
        blink = f % n in (7, 8)  # eyes shut mid-loop
        pupil_dx = round(3 * math.sin(2 * math.pi * f / n))  # pupils track left↔right
        for ex in (cx - 22, cx + 22):
            for x in range(ex - 8, ex + 8):
                for y in range(cy - 15, cy + 5):
                    if blink:
                        if abs(y - (cy - 5)) <= 1 and abs(x - ex) < 7:
                            px[x, y] = (70, 225, 95)
                        continue
                    if (x - ex) ** 2 + (y - (cy - 5)) ** 2 < 8**2:
                        px[x, y] = (70, 225, 95)
                    if (x - (ex + pupil_dx)) ** 2 + (y - (cy - 5)) ** 2 < 3**2:
                        px[x, y] = (12, 20, 14)  # dark pupil
        out.append(img)
    return out


def _matrix_frames() -> list[Any]:
    """Green rain whose columns fall (head y advances each frame, tail fades)."""
    from PIL import Image

    w, h, n = _SCENE_W, _SCENE_H, _SCENE_FRAMES
    tail = 46
    out: list[Any] = []
    for f in range(n):
        img = Image.new("RGB", (w, h), (0, 0, 0))
        px = img.load()
        for x in range(0, w, 6):
            speed = 6 + (x // 6) % 5
            head = ((x * 7) + f * speed * (h // n)) % (h + tail) - tail
            for t in range(tail):
                y = head - t
                if 0 <= y < h:
                    px[x, y] = (0, max(0, 255 - t * 6), 0)
        out.append(img)
    return out


def _generate_scenes(out_dir: Path) -> None:
    """Write three distinct *animated* GIF scenes mpv loops in each lab."""
    out_dir.mkdir(parents=True, exist_ok=True)
    _save_gif(_rainbow_frames(), out_dir / "rainbow.gif")
    _save_gif(_cat_frames(), out_dir / "cat.gif")
    _save_gif(_matrix_frames(), out_dir / "matrix.gif")


def _write_panels_config(path: Path, *, host: str, port: int) -> None:
    """Server config: N VNC-lease workers + N graphical (RFB) targets.

    The scenes are played natively inside each lab (see ``_start_scene_lab``), so
    the server only needs the lease workers the panes hijack and the RFB targets
    (the lab containers) it relays to — no ushell scene sessions.
    """
    blocks: list[str] = []
    for wk in nested.NEST_LEASE_WORKERS:
        blocks.append(
            f"""
[[sessions]]
session_id = "{wk}"
display_name = "VNC lease {wk}"
connector_type = "shell"
input_mode = "hijack"
auto_start = true
tags = ["vnc-lab"]
"""
        )
    for tgt, p in zip(nested.NEST_TARGETS, nested.NEST_PORTS, strict=True):
        blocks.append(
            f"""
[[graphical_targets]]
target_id = "{tgt}"
tenant_id = "lab"
protocol = "rfb"
target_address = "127.0.0.1:{p}"
name = "Nested RFB {tgt}"
enabled = true
width = 1280
height = 936
"""
        )
    body = f"""\
# Auto-generated by record_uterm_vnc_panels_demo.py — do not commit.
[server]
host = "{host}"
port = {port}
public_base_url = "http://127.0.0.1:{port}"
title = "uterm nautilus panels demo"

[auth]
mode = "header"
header_mode_acknowledged = true
principal_header = "x-uterm-principal"
role_header = "x-uterm-role"
worker_bearer_token = "{base.LAB_WORKER_TOKEN}"
trusted_proxy_ips = [
  "127.0.0.1", "::1",
  "192.168.5.1", "192.168.5.2", "192.168.65.1", "192.168.65.2",
  "172.17.0.1", "172.18.0.1", "10.0.0.1",
]

[ui]
app_path = "/app"
assets_path = "/_terminal"

[recording]
enabled_by_default = false

[security]
block_private_connector_targets = false
{"".join(blocks)}"""
    path.write_text(body, encoding="utf-8")


def _start_scene_lab(*, name: str, host_plain: int, gif_path: Path) -> None:
    """Start one lab container that plays *gif_path* natively via mpv.

    ``SCENE_MEDIA`` flips the entrypoint into scene mode (mpv loop) instead of
    launching Chromium. The clip is ``docker cp``-ed into ``/scene`` after boot
    rather than bind-mounted, because host scratchpad/evidence paths are not
    always shareable with the Docker VM; the entrypoint waits for it to land.
    """
    vnc_lab._remove_container(name)
    result = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--shm-size",
            vnc_lab.DEFAULT_SHM,
            "--add-host=host.docker.internal:host-gateway",
            "-p",
            f"{host_plain}:{vnc_lab.RFB_PLAIN_PORT}",
            "-e",
            "SCENE_MEDIA=/scene/scene.gif",
            "-e",
            f"GEOMETRY={nested.LAB_GEOMETRY}",
            vnc_lab.IMAGE_NAME,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker run {name} failed: {result.stderr.strip()[:400]}")
    # Create /scene and copy the clip in (docker cp needs no host file-sharing).
    subprocess.run(
        ["docker", "exec", name, "mkdir", "-p", "/scene"], capture_output=True, text=True, timeout=30, check=False
    )
    cp = subprocess.run(
        ["docker", "cp", str(gif_path.resolve()), f"{name}:/scene/scene.gif"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if cp.returncode != 0:
        raise RuntimeError(f"docker cp scene clip to {name} failed: {cp.stderr.strip()[:400]}")


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

    scenes_dir = evidence / "scenes"
    _generate_scenes(scenes_dir)
    gif_paths = [scenes_dir / fname for _sid, _label, fname in SCENES]

    server_port = base._free_port()
    cfg = evidence / "panels-server.toml"
    _write_panels_config(cfg, host="0.0.0.0", port=server_port)

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

        # Each lab natively plays a *different* scene (rainbow / cat / matrix) via
        # mpv, so the VNC panes are visibly distinct AND animate cleanly.
        for (name, port), gif_path in zip(zip(lab_names, nested.NEST_PORTS, strict=True), gif_paths, strict=True):
            _start_scene_lab(name=name, host_plain=port, gif_path=gif_path)
            if not nested._wait_lab_rfb(name):
                raise RuntimeError(f"lab {name} RFB never ready")
        lines.append("scene_labs_ready=ok")
        time.sleep(3.0)

        # Leases the panes hijack to view each RFB target.
        hids = [nested._acquire_lease(base_url, headers, w) for w in nested.NEST_LEASE_WORKERS]

        # Panels VNC pool = the three distinct-scene targets (fan-out).
        vnc_pool = ",".join(
            f"{w}~{h}~{t}" for w, h, t in zip(nested.NEST_LEASE_WORKERS, hids, nested.NEST_TARGETS, strict=True)
        )
        panels_url = f"{base_url}/_terminal/panels.html?vnc={vnc_pool}"
        lines.append(f"panels_url={panels_url}")

        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
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
            page.wait_for_function("() => (window.utermPanels?.leafCount ?? 0) >= 2", timeout=30_000)
            page.wait_for_function(
                "() => document.querySelector('.pane-vnc[data-state=\"connected\"]') !== null",
                timeout=40_000,
            )
            page.wait_for_timeout(2500)
            for _ in range(max(0, int(args.splits))):
                page.click("#panels-nautilus")
                page.wait_for_timeout(3000)
            # Let every pane finish connecting (12 noVNC sessions over 3 targets)
            # so the still shows live scenes, not "Connecting…"; bounded fallback.
            with suppress(PlaywrightTimeoutError):
                page.wait_for_function(
                    "() => { const p = window.utermPanels;"
                    " if (!p) return false;"
                    " const total = p.leafCount;"
                    " const ok = document.querySelectorAll('.pane-vnc[data-state=\"connected\"]').length;"
                    " return total > 0 && ok >= total; }",
                    timeout=20_000,
                )
            page.wait_for_timeout(1500)
            page.screenshot(path=str(full_png), full_page=True)
            metrics.update(
                page.evaluate(
                    "() => ({ panes: window.utermPanels?.leafCount ?? 0,"
                    " connectedVnc: document.querySelectorAll('.pane-vnc[data-state=\"connected\"]').length }) "
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
