#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""ffmpeg and asciinema helpers for demo recording scripts."""

from __future__ import annotations

import html as _html
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def asciinema_record(script_path: str | Path, out_path: Path) -> Path | None:
    """Record a terminal demo via asciinema. Returns output path or None."""
    try:
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "asciinema",
                "rec",
                str(out_path),
                "--overwrite",
                "-c",
                f"{sys.executable} {script_path} --run-demo",
            ],
            check=True,
            timeout=120,
        )
        return out_path
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"  [WARN] asciinema failed: {exc}", flush=True)
        return None


def ffmpeg_to_mp4(webm_path: Path) -> Path | None:
    """Convert a WebM to MP4 via ffmpeg. Returns mp4 path or None."""
    mp4_path = webm_path.with_suffix(".mp4")
    try:
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "ffmpeg",
                "-y",
                "-i",
                str(webm_path),
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                str(mp4_path),
            ],
            capture_output=True,
            check=True,
            timeout=120,
        )
        return mp4_path
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"  [WARN] ffmpeg_to_mp4 failed: {exc}", flush=True)
        return None


def trim_clip(src: Path | None, start_s: float, duration_s: float) -> Path | None:
    """Trim src to [start_s, start_s + duration_s]. Output written alongside src as *_trim.mp4."""
    if src is None or not src.exists():
        return None
    out = src.with_name(src.stem + "_trim.mp4")
    try:
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "ffmpeg",
                "-y",
                "-ss",
                str(start_s),
                "-i",
                str(src),
                "-t",
                str(duration_s),
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-pix_fmt",
                "yuv420p",
                str(out),
            ],
            capture_output=True,
            check=True,
            timeout=60,
        )
        return out
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"  [WARN] trim_clip failed: {exc}", flush=True)
        return None


def _video_dimensions(src: Path) -> tuple[int, int]:
    """Return (width, height) of a video file via ffprobe, or (1280, 720) on failure."""
    try:
        result = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "ffprobe",
                "-v",
                "quiet",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                str(src),
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
        parts = result.stdout.decode().strip().splitlines()[0].split(",")
        w_str, h_str = parts[0], parts[1]
        return int(w_str), int(h_str)
    except Exception:
        return 1280, 720


def add_title_card(
    src: Path | None,
    title: str,
    subtitle: str,
    duration_s: float = 1.5,
) -> Path | None:
    """Prepend a 1.5s title card to src. Returns *_titled.mp4 alongside src.

    Card style: GitHub dark (#0d1117) background, "Provide Terminal" wordmark
    in blue (#58a6ff), white feature title, grey subtitle.
    Uses Playwright to screenshot an HTML page to PNG, then ffmpeg for the video.
    """
    if src is None or not src.exists():
        return None
    from playwright.sync_api import sync_playwright

    w, h = _video_dimensions(src)
    tmp = Path(tempfile.mkdtemp())
    png_path = tmp / "card.png"
    card_mp4 = tmp / "card.mp4"
    list_file = tmp / "concat.txt"
    out = src.with_name(src.stem + "_titled.mp4")

    safe_title = _html.escape(title)
    safe_subtitle = _html.escape(subtitle)

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  background: #0d1117;
  display: flex; align-items: center; justify-content: center;
  width: {w}px; height: {h}px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
}}
.card {{ text-align: center; }}
.wordmark {{
  color: #58a6ff; font-size: 11px; letter-spacing: 4px;
  text-transform: uppercase; font-weight: 600; margin-bottom: 10px;
}}
.title {{ color: #e6edf3; font-size: 28px; font-weight: 700; margin-bottom: 8px; }}
.divider {{ width: 40px; height: 2px; background: #58a6ff; margin: 0 auto 10px; }}
.subtitle {{ color: #8b949e; font-size: 12px; }}
</style></head>
<body>
<div class="card">
  <div class="wordmark">Provide Terminal</div>
  <div class="title">{safe_title}</div>
  <div class="divider"></div>
  <div class="subtitle">{safe_subtitle}</div>
</div>
</body></html>"""

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": w, "height": h})
            page = ctx.new_page()
            page.set_content(html)
            page.screenshot(path=str(png_path))
            ctx.close()
            browser.close()

        # PNG → fixed-duration mp4
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "ffmpeg",
                "-y",
                "-loop",
                "1",
                "-i",
                str(png_path),
                "-c:v",
                "libx264",
                "-t",
                str(duration_s),
                "-pix_fmt",
                "yuv420p",
                "-r",
                "30",
                str(card_mp4),
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )

        # Concat card + src
        list_file.write_text(f"file '{card_mp4}'\nfile '{src}'\n")
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-pix_fmt",
                "yuv420p",
                str(out),
            ],
            capture_output=True,
            check=True,
            timeout=120,
        )
        return out
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"  [WARN] add_title_card failed: {exc}", flush=True)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def concat_clips(clips: list[Path | None], out: Path) -> Path | None:
    """Concatenate mp4 clips in order using ffmpeg concat demuxer."""
    valid = [c for c in clips if c is not None and c.exists()]
    if not valid:
        return None
    list_file = out.with_suffix(".concat_list.txt")
    list_file.write_text("\n".join(f"file '{c}'" for c in valid) + "\n")
    try:
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-vf",
                "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-pix_fmt",
                "yuv420p",
                str(out),
            ],
            capture_output=True,
            check=True,
            timeout=300,
        )
        return out
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"  [WARN] concat_clips failed: {exc}", flush=True)
        return None
    finally:
        list_file.unlink(missing_ok=True)
