#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""``cast`` command — fetches and replays an asciicast v2 (.cast) recording."""

from __future__ import annotations

import json as _json
from pathlib import Path

from provide.uterm.shell._output import PROMPT, error_msg
from provide.uterm.shell.commands.types import AnimatedResult


async def cmd_cast(arg: str) -> list[str] | AnimatedResult:
    """Fetch and replay an asciicast v2 (.cast) file."""
    tokens = arg.split()
    url = ""
    loop = False
    fps_override: float | None = None

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--loop":
            loop = True
            i += 1
        elif tok == "--fps" and i + 1 < len(tokens):
            fps_override = float(tokens[i + 1])
            i += 2
        elif not tok.startswith("--"):
            url = tok
            i += 1
        else:
            return [error_msg(f"unknown flag: {tok}") + PROMPT]

    if not url:
        return [error_msg("usage: cast [--fps N] [--loop] <url>") + PROMPT]

    # Fetch the cast file
    try:
        if url.startswith("file://"):
            file_path = Path(url[7:])
            if not file_path.is_file():
                return [error_msg(f"file not found: {file_path}") + PROMPT]
            text = file_path.read_text(encoding="utf-8", errors="replace")
        elif url.startswith(("http://", "https://")):
            import urllib.request

            req = urllib.request.Request(url, headers={"User-Agent": "provide-uterm/1.0"})  # noqa: S310
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310  # nosec B310
                text = resp.read().decode("utf-8", errors="replace")
        else:
            return [error_msg("unsupported URL scheme (use http://, https://, or file://)") + PROMPT]
    except Exception as exc:
        return [error_msg(f"cannot fetch: {exc}") + PROMPT]

    # Parse asciicast v2 format
    raw_lines = [ln for ln in text.splitlines() if ln.strip()]
    if not raw_lines:
        return [error_msg("empty cast file") + PROMPT]

    try:
        header = _json.loads(raw_lines[0])
        if header.get("version") != 2:
            return [error_msg(f"unsupported asciicast version: {header.get('version')}") + PROMPT]
    except Exception as exc:
        return [error_msg(f"invalid cast header: {exc}") + PROMPT]

    events: list[tuple[float, str]] = []
    for raw in raw_lines[1:]:
        try:
            ev = _json.loads(raw)
            if isinstance(ev, list) and len(ev) >= 3 and ev[1] == "o":
                events.append((float(ev[0]), str(ev[2])))
        except Exception:
            continue

    if not events:
        return [error_msg("no output events in cast file") + PROMPT]

    # Group events into time-bucketed frames at target FPS
    target_fps = fps_override if fps_override is not None else 15.0
    frame_dur = 1.0 / target_fps
    total_dur = events[-1][0] + frame_dur
    n_frames = max(1, int(total_dur / frame_dur))

    buckets: list[str] = [""] * n_frames
    for ts, data in events:
        idx = min(int(ts / frame_dur), n_frames - 1)
        buckets[idx] += data

    # Build frame list: clear screen first, then non-empty time slices
    frames: list[str] = ["\x1b[2J\x1b[H"]
    started = False
    for bucket in buckets:
        if bucket or started:
            started = True
            frames.append(bucket)

    if len(frames) <= 1:
        return [error_msg("cast file has no displayable output") + PROMPT]

    return AnimatedResult(frames=frames, fps=target_fps, loop=loop)
