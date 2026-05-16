#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""``render`` command — converts an image URL into ANSI-art frames."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

from provide.uterm.shell._output import PROMPT, error_msg
from provide.uterm.shell.commands.types import AnimatedResult


async def cmd_render(arg: str) -> list[str] | AnimatedResult:
    """Fetch *arg*'s URL and convert it to ANSI frames."""
    if not arg:
        return [
            error_msg("usage: render [--mode truecolor|256|16] [--cols N] [--rows N] [--fps N] [--loop] <url>") + PROMPT
        ]

    # Parse flags
    mode: Literal["truecolor", "256", "16"] = "truecolor"
    cols = 80
    rows = 24
    fps_override: float | None = None
    loop = False
    tokens = arg.split()
    url = ""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--mode" and i + 1 < len(tokens):
            _mode_raw = tokens[i + 1]
            if _mode_raw not in {"truecolor", "256", "16"}:
                return [error_msg(f"unknown mode {_mode_raw!r} (use truecolor, 256, or 16)") + PROMPT]
            mode = cast('Literal["truecolor", "256", "16"]', _mode_raw)
            i += 2
        elif tok == "--cols" and i + 1 < len(tokens):
            cols = int(tokens[i + 1])
            i += 2
        elif tok == "--rows" and i + 1 < len(tokens):
            rows = int(tokens[i + 1])
            i += 2
        elif tok == "--fps" and i + 1 < len(tokens):
            fps_override = float(tokens[i + 1])
            i += 2
        elif tok == "--loop":
            loop = True
            i += 1
        elif not tok.startswith("--"):
            url = tok
            i += 1
        else:
            return [error_msg(f"unknown flag: {tok}") + PROMPT]

    if not url:
        return [
            error_msg("usage: render [--mode truecolor|256|16] [--cols N] [--rows N] [--fps N] [--loop] <url>") + PROMPT
        ]

    # Fetch image bytes
    try:
        if url.startswith("file://"):
            file_path = Path(url[7:])
            if not file_path.is_file():
                return [error_msg(f"file not found: {file_path}") + PROMPT]
            data = file_path.read_bytes()
        elif url.startswith(("http://", "https://")):
            import urllib.request

            req = urllib.request.Request(url, headers={"User-Agent": "provide-uterm/1.0"})  # noqa: S310
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310  # nosec B310
                data = resp.read()
        else:
            return [error_msg("unsupported URL scheme (use http://, https://, or file://)") + PROMPT]
    except Exception as exc:
        return [error_msg(f"cannot fetch: {exc}") + PROMPT]

    # Convert to ANSI frames
    try:
        from provide.uterm.shell._render import image_to_ansi_frames

        frames, source_fps = image_to_ansi_frames(data, cols=cols, rows=rows, mode=mode)
    except ImportError as exc:
        return [error_msg(str(exc)) + PROMPT]
    except Exception as exc:
        return [error_msg(f"cannot decode image: {exc}") + PROMPT]

    fps_final = fps_override if fps_override is not None else source_fps

    if len(frames) <= 1 or fps_final <= 0:
        return [frames[0] + PROMPT] if frames else [error_msg("empty image") + PROMPT]

    return AnimatedResult(frames=frames, fps=fps_final, loop=loop)
