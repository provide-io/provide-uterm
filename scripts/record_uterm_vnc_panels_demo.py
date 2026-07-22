#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Record the recursive "nautilus" tiling panels page with distinct live scenes.

The full nesting on show is **VNC → browser → terminal → pixelated ANSI scene**:
each of the three lab RFB targets is a Chromium viewing a *different* ushell
session's ``terminal.html``, and each session ``render``s its GIF (a rainbow
gradient, the keyboard cat, matrix rain) as looping truecolor ANSI. xterm.js
repaints each streamed frame and — unlike a raw canvas — presents it to Xvfb, so
x11vnc captures the motion; the relay's update-driver then streams it on to the
noVNC panes. The nautilus panes fan out across the three targets, so the tiling
shows multiple *distinct* nested feeds that actually animate. Panes shrink as the
spiral winds inward via noVNC's scale-viewport.

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

# (ushell session id, display label, scene GIF filename). One per lab target so
# each VNC pane shows a visibly different *animated* scene, rendered as looping
# ANSI in that session's terminal.
SCENES = [
    ("scene-rainbow", "Rainbow", "rainbow.gif"),
    ("scene-cat", "Cat", "cat.gif"),
    ("scene-matrix", "Matrix", "matrix.gif"),
]

# Animated-GIF geometry. The GIF sources the frames the ushell `render` command
# turns into looping ANSI (its per-frame duration sets the playback cadence).
_SCENE_W, _SCENE_H = 320, 180
_SCENE_FRAMES = 16
_SCENE_DURATION_MS = 70

# The "cat" scene plays the real keyboard-cat GIF the non-VNC shell_render sample
# renders (fetched host-side at record time; falls back to a drawn cat offline).
_KEYBOARD_CAT_URL = "https://media.giphy.com/media/JIX9t2j0ZTN9S/giphy.gif"
# Letterbox the square source clip onto a ~4:3 canvas so the cat keeps its aspect
# when `render` samples it down to the terminal's character grid.
_CAT_CANVAS = (512, 374)


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


def _cat_piano_frames() -> list[Any]:
    """A cat playing a piano: paws bounce on the keys (which light up), the head
    bobs and blinks, and eighth-notes drift up. Drawn with ImageDraw for crisp
    shapes; every frame differs so it renders as a lively ANSI loop.
    """
    import math

    from PIL import Image, ImageDraw

    w, h, n = _SCENE_W, _SCENE_H, _SCENE_FRAMES
    fur, fur_lit = (120, 90, 64), (140, 105, 74)
    n_keys = 10
    kw = w / n_keys
    kb_top = h - 46
    out: list[Any] = []
    for f in range(n):
        img = Image.new("RGB", (w, h), (22, 20, 34))
        d = ImageDraw.Draw(img)
        # Hands alternate; each paw hovers over a key that shifts slightly.
        left_down = (f // 2) % 2 == 0
        left_key, right_key = 2 + (f // 4) % 2, 6 + (f // 4) % 2
        # White keys (pressed one lights up + dips).
        for i in range(n_keys):
            x0 = i * kw
            pressed = (i == left_key and left_down) or (i == right_key and not left_down)
            col = (255, 238, 130) if pressed else (236, 236, 242)
            d.rectangle([x0, kb_top + (3 if pressed else 0), x0 + kw - 2, h], fill=col, outline=(70, 70, 82))
        # Black keys (standard sharps pattern, skipping the E-F / B-C gaps).
        for i in range(n_keys - 1):
            if i % 7 in (0, 1, 3, 4, 5):
                bx = (i + 1) * kw - kw * 0.3
                d.rectangle([bx, kb_top, bx + kw * 0.6, kb_top + 26], fill=(24, 22, 30))
        # Cat, seated above the keys, bobbing to the beat.
        cx = w // 2
        bob = round(3 * math.sin(2 * math.pi * f / n))
        hy, hr = kb_top - 58 + bob, 30
        d.ellipse([cx - 26, hy + hr - 16, cx + 26, kb_top + 4], fill=fur)  # body
        d.polygon([(cx - hr + 6, hy - hr + 8), (cx - hr - 3, hy - hr - 20), (cx - 7, hy - hr + 4)], fill=fur)  # ear
        d.polygon([(cx + hr - 6, hy - hr + 8), (cx + hr + 3, hy - hr - 20), (cx + 7, hy - hr + 4)], fill=fur)  # ear
        d.ellipse([cx - hr, hy - hr, cx + hr, hy + hr], fill=fur)  # head
        blink = f % n == 7
        for ex in (cx - 12, cx + 12):
            if blink:
                d.line([ex - 6, hy - 3, ex + 6, hy - 3], fill=(70, 210, 100), width=2)
            else:
                d.ellipse([ex - 6, hy - 8, ex + 6, hy + 3], fill=(80, 230, 110))
                d.ellipse([ex - 2, hy - 4, ex + 2, hy + 1], fill=(15, 15, 20))
        d.polygon([(cx - 3, hy + 8), (cx + 3, hy + 8), (cx, hy + 12)], fill=(230, 150, 160))  # nose
        # Arms + paws reaching to the two keys (the pressing paw dips down).
        for key, down in ((left_key, left_down), (right_key, not left_down)):
            px = key * kw + kw / 2
            py = kb_top - 2 - (0 if down else 12)
            d.line([cx, hy + hr - 6, px, py], fill=fur, width=8)
            d.ellipse([px - 8, py - 8, px + 8, py + 8], fill=fur_lit)
        # Eighth-notes drifting up and looping.
        for k in range(3):
            phase = (f + k * 5) % n
            nx = cx + (k - 1) * 46 + round(6 * math.sin(phase))
            ny = hy - 34 - phase * 5
            note = (150, 210, 255)
            d.ellipse([nx - 4, ny - 3, nx + 4, ny + 3], fill=note)
            d.line([nx + 4, ny, nx + 4, ny - 14], fill=note, width=2)
            d.line([nx + 4, ny - 14, nx + 10, ny - 10], fill=note, width=2)
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


def _fetch_keyboard_cat_gif(out_path: Path) -> bool:
    """Fetch the real keyboard-cat GIF and letterbox it to the pane aspect.

    Returns True on success. Any network/decode failure returns False so the
    caller can fall back to the drawn cat (keeps the demo runnable offline).
    """
    import io
    import urllib.request

    try:
        req = urllib.request.Request(_KEYBOARD_CAT_URL, headers={"User-Agent": "Mozilla/5.0"})  # noqa: S310
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https giphy URL
            data = resp.read()
    except Exception as exc:  # offline / giphy hiccup → drawn-cat fallback
        print(f"keyboard-cat fetch failed ({exc}); using drawn cat", file=sys.stderr)
        return False

    from PIL import Image

    src = Image.open(io.BytesIO(data))
    cw, ch = _CAT_CANVAS
    frames: list[Any] = []
    durations: list[int] = []
    for i in range(getattr(src, "n_frames", 1)):
        src.seek(i)
        frame = src.convert("RGB")
        fitted = frame.copy()
        fitted.thumbnail((cw, ch), Image.LANCZOS)  # keep aspect, fit inside canvas
        canvas = Image.new("RGB", (cw, ch), (0, 0, 0))
        canvas.paste(fitted, ((cw - fitted.width) // 2, (ch - fitted.height) // 2))
        frames.append(canvas)
        durations.append(int(src.info.get("duration", 80) or 80))
    frames[0].save(out_path, save_all=True, append_images=frames[1:], duration=durations, loop=0, disposal=2)
    return True


def _generate_scenes(out_dir: Path) -> None:
    """Write three distinct *animated* GIF scenes the ushell render turns to ANSI."""
    out_dir.mkdir(parents=True, exist_ok=True)
    _save_gif(_rainbow_frames(), out_dir / "rainbow.gif")
    if not _fetch_keyboard_cat_gif(out_dir / "cat.gif"):
        _save_gif(_cat_piano_frames(), out_dir / "cat.gif")
    _save_gif(_matrix_frames(), out_dir / "matrix.gif")


def _write_panels_config(path: Path, *, host: str, port: int) -> None:
    """Server config: N ushell scene sessions + N VNC-lease workers + N RFB targets.

    Each scene is a ushell session that ``render``s its GIF as looping ANSI; a lab
    Chromium views that session's ``terminal.html``, and the panes mirror the lab
    over VNC — so the tiling is VNC → browser → terminal → the pixelated scene.
    """
    blocks: list[str] = []
    for sid, label, _fname in SCENES:
        blocks.append(
            f"""
[[sessions]]
session_id = "{sid}"
display_name = "{label}"
connector_type = "ushell"
input_mode = "hijack"
auto_start = true
tags = ["scene"]
"""
        )
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


def _drive_render(base_url: str, headers: dict[str, str], session: str, gif_path: Path) -> None:
    """Render *gif_path* as looping truecolor ANSI into the ushell *session*.

    ushell reports ``input_mode=open`` on hello, so switch it to hijack, acquire a
    lease, and feed ``render --loop``. A snapshot request afterwards re-caches the
    rendered screen so a browser connecting later opens straight onto the scene.
    xterm.js repaints each streamed frame (and, unlike a raw canvas, presents it to
    Xvfb), so the lab's terminal — and the VNC mirror of it — actually animates.
    """
    base.http_json("POST", f"{base_url}/worker/{session}/input_mode", headers=headers, body={"input_mode": "hijack"})
    hid = nested._acquire_lease(base_url, headers, session)
    base.send_shell_keys(
        base_url,
        headers,
        hid,
        f"render --loop --cols 96 --rows 30 file://{gif_path.resolve()}\r",
        session=session,
    )
    time.sleep(0.4)
    base.http_json("GET", f"{base_url}/worker/{session}/hijack/{hid}/snapshot", headers=headers)


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

        # Render each scene's GIF as looping ANSI into its ushell session.
        for (sid, _label, _fname), gif_path in zip(SCENES, gif_paths, strict=True):
            for _ in range(80):
                st, body = base.http_json("GET", f"{base_url}/api/sessions/{sid}", headers=headers)
                if st == 200 and isinstance(body, dict) and body.get("connected"):
                    break
                time.sleep(0.15)
            else:
                raise RuntimeError(f"scene session {sid} never connected")
            _drive_render(base_url, headers, sid, gif_path)
        time.sleep(1.5)
        lines.append("scenes_rendered=ok")

        # Each lab is a Chromium viewing a *different* scene's terminal.html, so the
        # tiling shows VNC → browser → terminal → the distinct pixelated ANSI scene.
        for (name, port), (sid, _label, _fname) in zip(
            zip(lab_names, nested.NEST_PORTS, strict=True), SCENES, strict=True
        ):
            demo_url = (
                f"http://{base._HOST_FROM_DOCKER}:{server_port}/_terminal/terminal.html?worker_id={sid}&role=browser"
            )
            nested._start_nested_lab(name=name, demo_url=demo_url, host_plain=port)
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
