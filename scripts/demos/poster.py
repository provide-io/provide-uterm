#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Extract a single still frame from an mp4 highlight for use as a poster image."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def extract_poster(mp4: Path, out: Path, at_seconds: float = 0.5) -> None:
    """Write one PNG frame from ``mp4`` at ``at_seconds`` into ``out``.

    Requires ``ffmpeg`` on ``PATH``. The demo recording pipeline already
    depends on ffmpeg, so this is a no-op extra dependency.
    """
    if shutil.which("ffmpeg") is None:
        msg = "ffmpeg not found on PATH; needed to extract poster frames."
        raise RuntimeError(msg)
    if not mp4.is_file():
        msg = f"source mp4 missing: {mp4}"
        raise FileNotFoundError(msg)
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{at_seconds:.3f}",
            "-i",
            str(mp4),
            "-frames:v",
            "1",
            str(out),
        ],
        check=True,
    )
