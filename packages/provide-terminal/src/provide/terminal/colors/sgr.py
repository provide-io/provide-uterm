#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""SGR parameter-list rewriting for color downgrade.

Given an SGR parameter list (the ``N;M;...`` part between ``ESC [`` and
``m``), scan for truecolor runs (``38;2;R;G;B`` foreground or
``48;2;R;G;B`` background) and replace them with the configured
lower-palette equivalent. Other SGR parameters (bold, italic, 256-color,
etc.) pass through unchanged.
"""

from __future__ import annotations

import re
from typing import Literal

from provide.terminal.colors.rgb import rgb_to_16_index, rgb_to_256

# SGR escape sequence: ``\x1b[`` ``params`` ``m`` where params is a
# possibly-empty semicolon-separated parameter list.
SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")

# Base ANSI 16-color foreground/background escape codes, indexed by palette
# index 0-15 (0-7 are the normal colors, 8-15 the bright variants).
_FG_16 = [30, 34, 32, 36, 31, 35, 33, 37, 90, 94, 92, 96, 91, 95, 93, 97]
_BG_16 = [40, 44, 42, 46, 41, 45, 43, 47, 100, 104, 102, 106, 101, 105, 103, 107]


def rewrite_params(params: str, mode: Literal["256", "16"]) -> str:
    """Rewrite an SGR parameter list, downgrading any truecolor runs.

    Walks the ``;``-separated parameters looking for the 5-parameter run
    ``38;2;R;G;B`` (foreground truecolor) or ``48;2;R;G;B`` (background
    truecolor). Each such run is replaced with its equivalent under the
    target ``mode``; everything else is preserved in place and order.

    Args:
        params: SGR parameter list (without the leading ``\\x1b[`` or
            trailing ``m``). May be empty.
        mode: ``"256"`` to map to xterm-256 cube, ``"16"`` to map to
            the base 16-color palette.

    Returns:
        A full SGR escape sequence (``\\x1b[<rewritten>m``) ready to be
        re-inserted into the stream.
    """
    if not params:
        return f"\x1b[{params}m"
    parts = params.split(";")
    out: list[str] = []
    i = 0
    n = len(parts)
    while i < n:
        if (
            i + 4 < n
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
                code = rgb_to_256(r, g, b)
                out.extend(["38" if is_fg else "48", "5", str(code)])
            else:
                idx = rgb_to_16_index(r, g, b)
                out.append(str(_FG_16[idx] if is_fg else _BG_16[idx]))
            i += 5
            continue
        out.append(parts[i])
        i += 1
    return f"\x1b[{';'.join(out)}m"


__all__ = ["SGR_RE", "rewrite_params"]
