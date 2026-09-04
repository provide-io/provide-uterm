#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: a live remote desktop, relayed through uterm's own VNC console.

The graphical stack has a feature page on the site and, until now, nothing
recorded behind it. This is the path that actually works today.

What it shows: a real X desktop inside the ``uterm-test-vnc`` lab, running
Chromium on the session's own text console, relayed byte-for-byte to the
first-party ``vnc.html`` in the browser. Upstream→browser is pumped raw;
browser→upstream passes the RFB input filter, which gates ``KeyEvent``,
``PointerEvent`` and ``ClientCutText`` on the same authorization callback the
rest of the control plane uses, and drops them when no callback is wired.

What it deliberately does NOT show: the seven ``gui_*`` tools driving this
desktop. They cannot. ``gui/attach`` refuses any protocol but ``memory``
(``rest_gui.py``: "RFB (VNC) client is deferred", 501), and the Go port asserts
the same refusal in ``TestGUIAttachWrongProtocol501``. The tool surface is real
and tested; the RFB client behind it is not written yet. A demo of an agent
clicking this desktop has to wait for that, and pretending otherwise would put
a video on the site that the code cannot produce.

The recording itself is delegated to ``scripts/record_uterm_vnc_demo_video.py``,
which already stands the lab up, drives live shell output through it, records
the console with Playwright, and refuses to publish a black frame. This module
adapts what that produces into the shape the demo manifest expects.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from scripts.demos import BASE_OUT, info, kv, ok, out_dir, trim_clip, warn

FEATURE = "graphical"
DESCRIPTION = (
    "A live X desktop relayed through uterm's VNC console, with the RFB input filter gating what the browser may inject"
)
TITLE = "Graphical Sessions"
SUBTITLE = "A remote desktop on the same control channel as a terminal"
HIGHLIGHT_START_S: float = 2.0
HIGHLIGHT_DURATION_S: float = 10.0

#: The relay recorder writes here, alongside the stills it has always produced.
_SOURCE_DIR = "vnc-lab"
_SOURCE_VIDEO = "uterm-vnc-text-demos.mp4"

#: Published under the feature's own name. ``_probe_artifacts`` falls back from
#: ``<name>_trim.mp4`` to ``<name>.mp4``, so the untrimmed capture is still
#: found if the trim step is skipped.
PRIMARY_VIDEO = "console_trim.mp4"

#: Seconds of desktop to capture. Long enough to show motion settling, short
#: enough that the highlight is most of the clip.
_CAPTURE_S = 16


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Run the relay recorder, then adapt its output into ``demo/graphical/``."""
    feat_dir = out_dir(FEATURE, base_out)
    repo_root = feat_dir.parent.parent

    info(f"Recording {_CAPTURE_S}s of the lab desktop through the VNC console...")
    # Anything already in the source dir predates this run. demo/vnc-lab keeps
    # committed stills from July, and copying those alongside a fresh capture
    # would publish screenshots of a different recording as if they were this
    # one's.
    started_at = time.time()
    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "record_uterm_vnc_demo_video.py"),
            "--seconds",
            str(_CAPTURE_S),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if result.returncode != 0:
        # The relay recorder writes its own diagnosis; surface the tail rather
        # than a bare exit code, then fail so the orchestrator marks a [SKIP]
        # instead of publishing a feature with no video.
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-8:]
        raise RuntimeError("record_uterm_vnc_demo_video failed:\n  " + "\n  ".join(tail))

    source = repo_root / "demo" / _SOURCE_DIR / _SOURCE_VIDEO
    if not source.is_file():
        raise RuntimeError(f"relay recorder reported success but produced no video at {source}")

    mp4_path = feat_dir / PRIMARY_VIDEO.replace("_trim.mp4", ".mp4")
    shutil.copy2(source, mp4_path)
    kv("console video", f"{mp4_path.name} ({mp4_path.stat().st_size // 1024} KiB)")

    shots_dir = feat_dir / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for shot in sorted((repo_root / "demo" / _SOURCE_DIR / "screenshots").glob("*.png")):
        if shot.stat().st_mtime < started_at:
            continue
        shutil.copy2(shot, shots_dir / shot.name)
        copied += 1
    if copied:
        kv("stills", f"{copied} written by this run")
    else:
        warn("this run produced no stills; only the video was published")

    highlight = trim_clip(mp4_path, HIGHLIGHT_START_S, HIGHLIGHT_DURATION_S)
    ok("graphical demo recorded — desktop relayed, filter enforced")
    return {"cast": None, "mp4": mp4_path, "highlight": highlight}
