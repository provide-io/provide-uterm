#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Compose a fleet/fanout grid video from per-worker clips.

The live iframe grid records blank because ``/app/session`` cannot be framed
(``X-Frame-Options: deny``) and, even with that header stripped, the in-iframe
terminal widget never mounts. So the grid composite is assembled here from the
per-worker videos (which render correctly on their own).
"""

from __future__ import annotations

import math
import subprocess  # nosec
from pathlib import Path


def tile_grid_with_footer(
    clips: list[Path | None],
    footer_src: Path | None,
    out: Path,
    *,
    cols: int,
    cell_w: int,
    cell_h: int,
    footer_h: int,
) -> Path | None:
    """Tile *clips* into a ``cols``-wide grid and stack a footer image below.

    Each clip is scaled+padded into a ``cell_w x cell_h`` cell; empty trailing
    cells are filled with the UI background. ``footer_src`` is a full grid
    screenshot whose bottom ``footer_h`` px (the broadcast-results panel) is
    cropped and appended under the tiled grid. Falls back to the tiles-only
    video if the footer compose fails, and returns ``None`` if tiling fails.
    """
    valid = [c for c in clips if c is not None and c.exists()]
    n = len(valid)
    if n == 0:
        return None
    rows = math.ceil(n / cols)
    width = cell_w * cols

    inputs: list[str] = []
    filters: list[str] = []
    for i, clip in enumerate(valid):
        inputs.extend(["-i", str(clip)])
        filters.append(
            f"[{i}:v]scale={cell_w}:{cell_h}:force_original_aspect_ratio=decrease,"
            f"pad={cell_w}:{cell_h}:(ow-iw)/2:(oh-ih)/2:color=#0d1117,setsar=1[v{i}]"
        )
    filters.extend(f"color=c=#0d1117:s={cell_w}x{cell_h}:d=1,setsar=1[v{i}]" for i in range(n, cols * rows))
    row_labels: list[str] = []
    for r in range(rows):
        row_cells = "".join(f"[v{r * cols + c}]" for c in range(cols))
        filters.append(f"{row_cells}hstack=inputs={cols}[row{r}]")
        row_labels.append(f"[row{r}]")
    if rows > 1:
        filters.append(f"{''.join(row_labels)}vstack=inputs={rows}[grid]")
        grid_label = "[grid]"
    else:
        grid_label = row_labels[0]

    tiled = out.with_name(out.stem + "_tiled.mp4")
    enc = ["-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p"]
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                *inputs,
                "-filter_complex",
                ";".join(filters),
                "-map",
                grid_label,
                *enc,
                "-shortest",
                str(tiled),
            ],
            capture_output=True,
            check=True,
            timeout=600,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"  [WARN] tile_grid_with_footer tiling failed: {exc}", flush=True)
        return None

    if footer_src is None or not Path(footer_src).exists():
        tiled.replace(out)
        return out
    footer_png = out.with_name(out.stem + "_footer.png")
    tile_h = cell_h * rows
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(footer_src),
                "-vf",
                f"crop=iw:{footer_h}:0:ih-{footer_h},scale={width}:{footer_h}",
                str(footer_png),
            ],
            capture_output=True,
            check=True,
            timeout=60,
        )
        # Overlay the (still) footer onto a padded canvas. Overlaying a single
        # frame keeps the output the same length as the tiled video — unlike
        # looping the image as a second input, which is unbounded in duration.
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(tiled),
                "-i",
                str(footer_png),
                "-filter_complex",
                (
                    f"[0:v]pad={width}:{tile_h + footer_h}:0:0:color=#0d1117[bg];"
                    f"[1:v]setsar=1[ftr];[bg][ftr]overlay=0:{tile_h}[out]"
                ),
                "-map",
                "[out]",
                *enc,
                str(out),
            ],
            capture_output=True,
            check=True,
            timeout=600,
        )
        return out
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"  [WARN] tile_grid_with_footer footer compose failed: {exc}; using tiles only", flush=True)
        tiled.replace(out)
        return out
