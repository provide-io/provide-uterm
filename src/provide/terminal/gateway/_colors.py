#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""ANSI color downgrade utilities for gateway streams."""

from __future__ import annotations

import re

_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")


def _clamp8(v: int) -> int:
    return 0 if v < 0 else 255 if v > 255 else v


def _rgb_to_256(r: int, g: int, b: int) -> int:
    if r == g == b:
        if r < 8:
            return 16
        if r > 248:
            return 231
        return 232 + int((r - 8) / 247 * 24)
    rc = round(_clamp8(r) / 255 * 5)
    gc = round(_clamp8(g) / 255 * 5)
    bc = round(_clamp8(b) / 255 * 5)
    return 16 + 36 * rc + 6 * gc + bc


_FG_16 = [30, 34, 32, 36, 31, 35, 33, 37, 90, 94, 92, 96, 91, 95, 93, 97]
_BG_16 = [40, 44, 42, 46, 41, 45, 43, 47, 100, 104, 102, 106, 101, 105, 103, 107]


def _rgb_to_16_index(r: int, g: int, b: int) -> int:
    table = [
        (0, 0, 0),
        (0, 0, 205),
        (0, 205, 0),
        (0, 205, 205),
        (205, 0, 0),
        (205, 0, 205),
        (205, 205, 0),
        (229, 229, 229),
        (127, 127, 127),
        (92, 92, 255),
        (92, 255, 92),
        (92, 255, 255),
        (255, 92, 92),
        (255, 92, 255),
        (255, 255, 92),
        (255, 255, 255),
    ]
    best_i, best_d = 0, 10**9
    rr, gg, bb = _clamp8(r), _clamp8(g), _clamp8(b)
    for i, (tr, tg, tb) in enumerate(table):
        d = (rr - tr) * (rr - tr) + (gg - tg) * (gg - tg) + (bb - tb) * (bb - tb)
        if d < best_d:
            best_d = d
            best_i = i
    return best_i


def _apply_color_mode(raw: bytes, mode: str) -> bytes:
    """Downgrade truecolor ANSI SGR codes to 256-color or 16-color.

    mode: "passthrough" | "256" | "16"
    """
    if mode == "passthrough":
        return raw

    text = raw.decode("latin-1", errors="replace")

    def rewrite_sgr(match: re.Match[str]) -> str:
        params = match.group(1)
        if not params:
            return match.group(0)
        parts = params.split(";")
        out: list[str] = []
        i = 0
        while i < len(parts):
            if (
                i + 4 < len(parts)
                and parts[i] in {"38", "48"}
                and parts[i + 1] == "2"
                and parts[i + 2].isdigit()
                and parts[i + 3].isdigit()
                and parts[i + 4].isdigit()
            ):
                r = int(parts[i + 2])
                g = int(parts[i + 3])
                b = int(parts[i + 4])
                is_fg = parts[i] == "38"
                if mode == "256":
                    code = _rgb_to_256(r, g, b)
                    out.extend(["38" if is_fg else "48", "5", str(code)])
                else:
                    idx = _rgb_to_16_index(r, g, b)
                    out.append(str(_FG_16[idx] if is_fg else _BG_16[idx]))
                i += 5
                continue
            out.append(parts[i])
            i += 1
        return f"\x1b[{';'.join(out)}m"

    return _SGR_RE.sub(rewrite_sgr, text).encode("latin-1", errors="replace")
