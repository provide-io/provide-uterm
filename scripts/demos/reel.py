#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Super reel: assemble all 14 feature highlights into a single video."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from scripts.demos.ffmpeg import add_title_card, concat_clips, trim_clip
from scripts.demos.output import BASE_OUT, info, ok, warn

DEMO_MODULES = [
    "record_pty",
    "record_shell_render",
    "record_annotation",
    "record_recording",
    "record_replay",
    "record_ssh",
    "record_telnet",
    "record_tunnel",
    "record_http_inspect",
    "record_mcp",
    "record_hijack",
    "record_deckmux",
    "record_fleet",
    "record_fanout",
]

# Primary mp4 key in the record() return dict for each demo.
# Used when no "highlight" key is present (e.g. cached runs from before this plan).
_PRIMARY_KEY: dict[str, str] = {
    "record_fleet": "grid",
    "record_fanout": "grid",
    "record_hijack": "operator_mp4",
    "record_deckmux": "composite",
    "record_tunnel": "control_mp4",
}


def record_reel(base_out: Path = BASE_OUT, *, force: bool = False) -> Path | None:
    """Run all 14 demos (skipping cached), collect highlights, build super reel.

    Skips re-recording demos whose output already exists unless force=True.
    Returns path to demo/recordings/reel.mp4 or None on failure.
    """
    titled_clips: list[Path] = []

    for mod_name in DEMO_MODULES:
        mod = importlib.import_module(f"scripts.demos.{mod_name}")
        feature: str = mod.FEATURE
        title: str = mod.TITLE
        subtitle: str = mod.SUBTITLE
        start_s: float = mod.HIGHLIGHT_START_S
        duration_s: float = mod.HIGHLIGHT_DURATION_S
        feat_dir = base_out / feature
        # Default key resolves to ``browser_trim.mp4`` / ``browser.mp4`` —
        # the filenames every single-browser recorder produces. The earlier
        # default of ``"mp4"`` mapped to a literal ``mp4_trim.mp4`` lookup
        # that never matched any real file, so the cached-highlight branch
        # silently never hit and every single-browser demo got re-recorded
        # on every reel rebuild.
        primary_key = _PRIMARY_KEY.get(mod_name, "browser_mp4")

        # Check for existing highlight or primary mp4
        existing_highlight = feat_dir / f"{primary_key.replace('_mp4', '')}_trim.mp4"
        primary_mp4 = feat_dir / f"{primary_key.replace('_mp4', '')}.mp4"

        if not force and existing_highlight.exists():
            info(f"{feature}: using cached highlight")
            highlight: Path | None = existing_highlight
        elif not force and primary_mp4.exists():
            info(f"{feature}: trimming from cached primary mp4")
            highlight = trim_clip(primary_mp4, start_s, duration_s)
        else:
            info(f"{feature}: recording...")
            result = mod.record(base_out)
            highlight = result.get("highlight")
            if highlight is None:
                # Fall back: trim primary mp4 from result
                raw = result.get(primary_key)
                if raw is not None:
                    highlight = trim_clip(Path(str(raw)), start_s, duration_s)

        if highlight is None or not Path(str(highlight)).exists():
            warn(f"{feature}: no highlight clip produced, skipping")
            continue

        titled = add_title_card(Path(str(highlight)), title, subtitle)
        if titled is None:
            warn(f"{feature}: add_title_card failed, using raw clip")
            titled = Path(str(highlight))

        titled_clips.append(titled)
        ok(f"{feature}: {len(titled_clips)}/{len(DEMO_MODULES)} clips ready")

    if not titled_clips:
        warn("No clips collected — reel not created")
        return None

    reel_out = base_out / "reel.mp4"
    info(f"Assembling reel from {len(titled_clips)} clips...")
    result_path = concat_clips(titled_clips, reel_out)
    if result_path:
        ok(f"Reel saved → {result_path}")
    return result_path


if __name__ == "__main__":
    force_flag = "--force" in sys.argv
    record_reel(force=force_flag)
