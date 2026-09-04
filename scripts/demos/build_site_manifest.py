#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Build a JSON catalog of recorded demos for consumption by site-uterm-io.

Walks the demo output tree, imports each ``record_<feature>`` module to read its
metadata constants (``FEATURE``, ``TITLE``, ``SUBTITLE``, highlight bounds, and the
optional ``SITE_FORMAT``), inspects which artifacts exist under ``demo/<feature>/``,
optionally extracts a poster PNG from the highlight mp4, and writes the catalog to
``demo/site-manifest.json``.

Run via the ``--posters`` flag to also (re)generate ``poster.png`` next to each
``browser_trim.mp4``.

Usage::

    uv run python -m scripts.demos.build_site_manifest [--posters]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import importlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

from scripts.demos.output import BASE_OUT
from scripts.demos.poster import extract_poster

log = logging.getLogger(__name__)

# The 17 demos that have ``record_<feature>.py`` modules. Sorted so the manifest
# emits a deterministic order. Adding a new demo means appending its feature id
# here AND shipping a recorder module with the standard metadata constants.
FEATURES: tuple[str, ...] = (
    "annotation",
    "deckmux",
    "demo_grid",
    "fanout",
    "fleet",
    "graphical",
    "gui_agent",
    "hijack",
    "http_inspect",
    "mcp",
    "pty",
    "recording",
    "replay",
    "shell_render",
    "ssh",
    "telnet",
    "tunnel",
)


def _load_meta(feature: str) -> dict[str, Any]:
    """Import ``scripts.demos.record_<feature>`` and harvest its metadata constants."""
    module_name = f"scripts.demos.record_{feature}"
    module = importlib.import_module(module_name)

    def _attr(name: str, default: Any = None) -> Any:
        return getattr(module, name, default)

    return {
        "id": _attr("FEATURE", feature),
        "title": _attr("TITLE", feature.replace("_", " ").title()),
        "subtitle": _attr("SUBTITLE", ""),
        "description": _attr("DESCRIPTION", ""),
        "duration_seconds": float(_attr("HIGHLIGHT_DURATION_S", 0.0)),
        "highlight_start_s": float(_attr("HIGHLIGHT_START_S", 0.0)),
        "format": _attr("SITE_FORMAT", "mp4"),
        "primary_video": _attr("PRIMARY_VIDEO", "browser_trim.mp4"),
    }


def _probe_artifacts(feature_dir: Path, primary_video: str = "browser_trim.mp4") -> dict[str, Any]:
    """Inspect a ``demo/<feature>/`` directory and report available artifacts.

    Paths are relative to ``BASE_OUT`` so the manifest stays portable across
    machines (the consumer joins with its own ``UTERM_DIR`` root).

    ``primary_video`` overrides the default ``browser_trim.mp4`` filename
    that single-browser recorders produce — multi-browser demos
    (hijack/deckmux/fleet/fanout) write under names like
    ``operator_trim.mp4`` or ``composite_trim.mp4`` and declare that via
    their recorder module's ``PRIMARY_VIDEO`` constant.
    """
    artifacts: dict[str, Any] = {}
    relative = feature_dir.name

    video = feature_dir / primary_video
    if not video.is_file():
        # Fall back to the un-trimmed primary recording when the trim step
        # hasn't been run yet. The consumer site can still ship it. The
        # naming convention is ``<base>_trim.mp4`` ⇄ ``<base>.mp4``.
        untrimmed_name = primary_video.replace("_trim.mp4", ".mp4")
        video = feature_dir / untrimmed_name
    if not video.is_file():
        # Last-ditch fallback: a recorder may have changed naming without
        # updating PRIMARY_VIDEO — search for any *_trim.mp4 in the dir.
        trims = sorted(feature_dir.glob("*_trim.mp4"))
        if trims:
            video = trims[0]
    if video.is_file():
        artifacts["video"] = f"{relative}/{video.name}"

    poster = feature_dir / "poster.png"
    if poster.is_file():
        artifacts["poster"] = f"{relative}/poster.png"

    cast = feature_dir / "terminal.cast"
    if cast.is_file():
        artifacts["cast"] = f"{relative}/terminal.cast"

    composite = feature_dir / "composite.mp4"
    if composite.is_file():
        artifacts["composite"] = f"{relative}/composite.mp4"

    screenshots_dir = feature_dir / "screenshots"
    if screenshots_dir.is_dir():
        # Only when it holds something. out_dir() creates the directory for
        # every demo, so a cast-only feature was publishing an empty gallery.
        shots = sorted(
            f"{relative}/screenshots/{shot.name}"
            for shot in screenshots_dir.iterdir()
            if shot.is_file() and shot.suffix.lower() in {".png", ".jpg", ".jpeg"}
        )
        if shots:
            artifacts["screenshots"] = shots

    return artifacts


def _maybe_make_poster(feature_dir: Path, primary_video: str = "browser_trim.mp4") -> Path | None:
    """If a highlight mp4 is present and no poster exists, render one and return its path."""
    mp4 = feature_dir / primary_video
    if not mp4.is_file():
        mp4 = feature_dir / primary_video.replace("_trim.mp4", ".mp4")
    if not mp4.is_file():
        return None

    poster = feature_dir / "poster.png"
    if poster.is_file():
        return poster

    extract_poster(mp4, poster, at_seconds=0.5)
    return poster


def build_manifest(*, generate_posters: bool = False) -> dict[str, Any]:
    """Build the manifest dict, optionally regenerating posters as a side effect."""
    repo_root = Path(__file__).resolve().parents[2]
    base_out = (repo_root / BASE_OUT).resolve()

    demos: list[dict[str, Any]] = []
    for feature in FEATURES:
        feature_dir = base_out / feature
        if not feature_dir.is_dir():
            log.warning("skipping %s: no directory at %s", feature, feature_dir)
            continue

        meta = _load_meta(feature)
        primary_video = meta.pop("primary_video", "browser_trim.mp4")

        if generate_posters and meta["format"] == "mp4":
            try:
                _maybe_make_poster(feature_dir, primary_video)
            except (RuntimeError, FileNotFoundError) as exc:
                log.warning("poster for %s skipped: %s", feature, exc)

        meta["artifacts"] = _probe_artifacts(feature_dir, primary_video)

        # Graceful downgrade: when an mp4-format demo's video failed to record
        # but the asciinema cast made it, surface the cast on the site rather
        # than rendering a "no video" fallback.
        if meta["format"] == "mp4" and "video" not in meta["artifacts"] and "cast" in meta["artifacts"]:
            log.info("%s: downgrading mp4 → cast (no video recorded)", feature)
            meta["format"] = "cast"

        demos.append(meta)

    return {
        "generated_at": _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "demos": demos,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--posters",
        action="store_true",
        help="Extract a poster.png from each mp4 highlight if missing.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Manifest output path (default: demo/site-manifest.json).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    manifest = build_manifest(generate_posters=args.posters)

    repo_root = Path(__file__).resolve().parents[2]
    out_path = args.out or (repo_root / BASE_OUT / "site-manifest.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out_path} ({len(manifest['demos'])} demos)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
