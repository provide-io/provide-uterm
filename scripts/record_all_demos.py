#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Orchestrator: run all 17 per-feature demo recordings and write INDEX.md.

Usage:
    uv run python scripts/record_all_demos.py
    uv run python scripts/record_all_demos.py --features fanout,annotation
"""

from __future__ import annotations

import importlib
import sys
import time
import traceback
from pathlib import Path

# Ensure the repo root is on sys.path so `scripts.demos.*` modules are importable
# when this orchestrator is run directly (e.g. `uv run python scripts/record_all_demos.py`).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Must match ``scripts.demos.output.BASE_OUT``: each recorder defaults to that
# path, and build_site_manifest.py probes ``<BASE_OUT>/<feature>/`` to decide
# which artifacts a demo has. This orchestrator passed its own value into
# ``record()``, so a full run wrote every video to demo/recordings/<feature>/ --
# a tree the manifest builder never looks at. The manifest then saw the old
# demo/<feature>/ contents, found no mp4, and downgraded the demo to a cast.
# Imported after the sys.path insert above rather than at the top of the file,
# which is why this is not a top-level import; the pairing is pinned by
# tests/scripts/test_demo_recorder_metadata.py.
BASE_OUT = Path("demo")

# (module_name, feature_key, description)
# (module_name, feature_key). The description is NOT repeated here: it is
# ``DESCRIPTION`` on the recorder module, which build_site_manifest.py also
# reads. Carrying a second copy meant INDEX.md kept printing that the tunnel
# demo showed a Cloudflare Worker path long after record_tunnel.py said, in
# its own docstring, that no external Worker is involved.
FEATURES: list[tuple[str, str]] = [
    ("scripts.demos.record_fanout", "fanout"),
    ("scripts.demos.record_annotation", "annotation"),
    ("scripts.demos.record_recording", "recording"),
    ("scripts.demos.record_pty", "pty"),
    ("scripts.demos.record_ssh", "ssh"),
    ("scripts.demos.record_hijack", "hijack"),
    ("scripts.demos.record_deckmux", "deckmux"),
    ("scripts.demos.record_graphical", "graphical"),
    ("scripts.demos.record_gui_agent", "gui_agent"),
    ("scripts.demos.record_demo_grid", "demo_grid"),
    ("scripts.demos.record_shell_render", "shell_render"),
    ("scripts.demos.record_replay", "replay"),
    ("scripts.demos.record_mcp", "mcp"),
    ("scripts.demos.record_telnet", "telnet"),
    ("scripts.demos.record_http_inspect", "http_inspect"),
    ("scripts.demos.record_tunnel", "tunnel"),
    ("scripts.demos.record_fleet", "fleet"),
]


def _parse_feature_filter() -> set[str] | None:
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg.startswith("--features="):
            return set(arg.split("=", 1)[1].split(","))
        if arg == "--features" and i + 1 < len(args):
            return set(args[i + 1].split(","))
    return None


def _write_index(results: dict[str, dict[str, Path | None]], descriptions: dict[str, str]) -> Path:
    index_path = BASE_OUT / "INDEX.md"
    lines = [
        "# provide-uterm Feature Demos",
        "",
        "One recording set per feature — terminal cast (asciinema) + browser video (MP4).",
        "",
        "| Feature | Terminal | Browser | Description |",
        "|---------|----------|---------|-------------|",
    ]
    for feature, paths in results.items():
        cast = paths.get("cast")
        desc = descriptions.get(feature, "")
        cast_link = f"[cast]({feature}/terminal.cast)" if cast else "(recording failed)"

        # Collect all video links — mp4, or any key ending in _mp4
        video_links: list[str] = []
        for key, path in paths.items():
            if path and (key == "mp4" or key.endswith("_mp4")):
                label = "video" if key == "mp4" else key.replace("_mp4", "")
                filename = path.name
                video_links.append(f"[{label}]({feature}/{filename})")
        mp4_cell = " ".join(video_links) if video_links else "(recording failed)"

        lines.append(f"| {feature} | {cast_link} | {mp4_cell} | {desc} |")
    lines.append("")
    index_path.write_text("\n".join(lines))
    return index_path


def main() -> None:
    filter_features = _parse_feature_filter()
    BASE_OUT.mkdir(parents=True, exist_ok=True)

    print("\n\033[1;35m╔══════════════════════════════════════════════════╗\033[0m")
    print("\033[1;35m║   provide-uterm Per-Feature Demo Recording    ║\033[0m")
    print("\033[1;35m╚══════════════════════════════════════════════════╝\033[0m\n")

    results: dict[str, dict[str, Path | None]] = {}
    descriptions: dict[str, str] = {}

    for mod_name, feature in FEATURES:
        if filter_features and feature not in filter_features:
            continue
        t0 = time.monotonic()
        try:
            mod = importlib.import_module(mod_name)
            description = getattr(mod, "DESCRIPTION", "")
            descriptions[feature] = description
            print(f"\033[1;36m[{feature}]\033[0m {description}")
            paths = mod.record(BASE_OUT)
            elapsed = time.monotonic() - t0
            results[feature] = paths
            cast_ok = "✓" if paths.get("cast") else "✗"
            video_keys = [k for k in paths if k == "mp4" or k.endswith("_mp4")]
            mp4_detail = " ".join(f"{k}=✓" for k in video_keys if paths.get(k)) or "mp4=✗"
            print(f"  cast={cast_ok} {mp4_detail} ({elapsed:.1f}s)\n")
        except Exception:
            elapsed = time.monotonic() - t0
            print(f"  \033[31m[SKIP] {feature}: error after {elapsed:.1f}s\033[0m")
            traceback.print_exc()
            results[feature] = {"cast": None, "mp4": None}
            print()

    index_path = _write_index(results, descriptions)
    print("\n\033[1;35m=== Complete ===\033[0m")
    print(f"  INDEX: {index_path}")
    print(f"\n  Files in {BASE_OUT}/:")
    for feat_dir in sorted(BASE_OUT.iterdir()):
        if feat_dir.is_dir():
            files = list(feat_dir.rglob("*"))
            total = sum(f.stat().st_size for f in files if f.is_file())
            print(f"    {feat_dir.name:20s} {total:>10,} bytes")


if __name__ == "__main__":
    main()
