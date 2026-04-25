#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Orchestrator: run all 14 per-feature demo recordings and write INDEX.md.

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

BASE_OUT = Path("demo/recordings")

# (module_name, feature_key, description)
FEATURES: list[tuple[str, str, str]] = [
    (
        "scripts.demos.record_fanout",
        "fanout",
        "Broadcast a command to 3 sessions simultaneously, show per-node output and divergence detection",
    ),
    (
        "scripts.demos.record_annotation",
        "annotation",
        "Agent self-annotation and automatic detection of 20 security/lifecycle patterns",
    ),
    (
        "scripts.demos.record_recording",
        "recording",
        "Enable session recording, produce terminal activity, download JSONL recording file",
    ),
    ("scripts.demos.record_pty", "pty", "Spawn a local PTY session, run commands, show resize and snapshot"),
    ("scripts.demos.record_ssh", "ssh", "Connect a session to an SSH host, run commands, show live output"),
    (
        "scripts.demos.record_hijack",
        "hijack",
        "Viewer connects read-only, operator takes exclusive control, admin force-reclaims",
    ),
    (
        "scripts.demos.record_deckmux",
        "deckmux",
        "Multiple operator cursors join the same session, presence state is broadcast",
    ),
    (
        "scripts.demos.record_shell_render",
        "shell_render",
        "Send an image URL to the shell render command, get ANSI truecolor art back",
    ),
    (
        "scripts.demos.record_replay",
        "replay",
        "Record 10 seconds of terminal activity then scrub through replay in the browser",
    ),
    (
        "scripts.demos.record_mcp",
        "mcp",
        "21 MCP tools for AI agent integration: session management, hijack, fan-out, annotation",
    ),
    (
        "scripts.demos.record_telnet",
        "telnet",
        "Connect a session to a local telnet server, show negotiation and live output",
    ),
    (
        "scripts.demos.record_http_inspect",
        "http_inspect",
        "Proxy HTTP traffic through uterm inspect tunnel, inspect requests/responses in browser",
    ),
    (
        "scripts.demos.record_tunnel",
        "tunnel",
        "Session served through local CF Worker (wrangler dev --local), showing the CF path",
    ),
    (
        "scripts.demos.record_fleet",
        "fleet",
        "Spawn 3 fleet shell workers, register with the External Management Tier, broadcast a deploy command",
    ),
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
        "# provide-terminal Feature Demos",
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
    print("\033[1;35m║   provide-terminal Per-Feature Demo Recording    ║\033[0m")
    print("\033[1;35m╚══════════════════════════════════════════════════╝\033[0m\n")

    results: dict[str, dict[str, Path | None]] = {}
    descriptions: dict[str, str] = {}

    for mod_name, feature, description in FEATURES:
        if filter_features and feature not in filter_features:
            continue
        descriptions[feature] = description
        print(f"\033[1;36m[{feature}]\033[0m {description}")
        t0 = time.monotonic()
        try:
            mod = importlib.import_module(mod_name)
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
