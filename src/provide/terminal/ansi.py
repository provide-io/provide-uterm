#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""ANSI color code conversion for TW2002 terminal output.

The C server uses {+c} for bright cyan, {-x} for reset, etc.
This module converts those tags to standard ANSI escape sequences.

Also provides color-upgrade utilities (16-color → 256-color / truecolor) and
preview_ansi() for rendering mixed BBS color tokens to standard ANSI escapes.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# BBS color palette constants
# ---------------------------------------------------------------------------

# 256-color palette indices that map the 16 base BBS colors
DEFAULT_PALETTE: list[int] = [
    0,  # black
    160,  # red
    34,  # green
    184,  # yellow/brown
    27,  # blue
    127,  # magenta
    37,  # cyan
    252,  # white
    244,  # bright black / gray
    196,  # bright red
    46,  # bright green
    226,  # bright yellow
    51,  # bright blue
    201,  # bright magenta
    87,  # bright cyan
    231,  # bright white
]

# Direct RGB tuples for the 16 base BBS colors (truecolor output)
DEFAULT_RGB: list[tuple[int, int, int]] = [
    (0, 0, 0),  # black
    (215, 0, 0),  # red
    (0, 175, 0),  # green
    (215, 175, 0),  # yellow/brown
    (0, 95, 255),  # blue
    (175, 0, 175),  # magenta
    (0, 175, 175),  # cyan
    (208, 208, 208),  # white
    (128, 128, 128),  # bright black / gray
    (255, 0, 0),  # bright red
    (0, 255, 0),  # bright green
    (255, 255, 0),  # bright yellow
    (0, 175, 255),  # bright blue
    (255, 0, 255),  # bright magenta
    (95, 255, 255),  # bright cyan
    (255, 255, 255),  # bright white
]

# ---------------------------------------------------------------------------
# Private regex patterns
# ---------------------------------------------------------------------------

_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
_TOKEN_RE = re.compile(r"\{([PT])(\d{1,3})\}")
_EXT_TOKEN_RE = re.compile(r"\{([FBPT])(\d{1,3})\}")

# Mapping from TW2002 color tags to ANSI escape sequences
COLOR_MAP: dict[str, str] = {
    "{+c}": "\033[1;36m",  # Bright cyan
    "{-c}": "\033[0;36m",  # Normal cyan
    "{+r}": "\033[1;31m",  # Bright red
    "{-r}": "\033[0;31m",  # Normal red
    "{+g}": "\033[1;32m",  # Bright green
    "{-g}": "\033[0;32m",  # Normal green
    "{+y}": "\033[1;33m",  # Bright yellow
    "{-y}": "\033[0;33m",  # Normal yellow
    "{+b}": "\033[1;34m",  # Bright blue
    "{-b}": "\033[0;34m",  # Normal blue
    "{+m}": "\033[1;35m",  # Bright magenta
    "{-m}": "\033[0;35m",  # Normal magenta
    "{+w}": "\033[1;37m",  # Bright white
    "{+Bw}": "\033[1;37m",  # Bold white (TWGS header style)
    "{-w}": "\033[0;37m",  # Normal white
    "{+k}": "\033[1;30m",  # Bright black (gray)
    "{-k}": "\033[0;30m",  # Normal black
    "{-x}": "\033[0m",  # Reset
}

# Common ANSI sequences
CLEAR_SCREEN: str = "\033[2J\033[H"
BOLD: str = "\033[1m"
RESET: str = "\033[0m"


def colorize(text: str) -> str:
    """Convert TW2002 color tags to ANSI escape sequences.

    Replaces all occurrences of {+X} and {-X} tags with their corresponding
    ANSI escape codes. Unknown tags are left as-is.

    Args:
        text: String containing TW2002 color tags.

    Returns:
        String with ANSI escape sequences substituted.
    """
    result = text
    for tag, ansi in COLOR_MAP.items():
        result = result.replace(tag, ansi)
    return result


def strip_colors(text: str) -> str:
    """Remove all TW2002 color tags from text.

    Useful for logging or plain-text output where ANSI codes are unwanted.

    Args:
        text: String containing TW2002 color tags.

    Returns:
        String with all color tags removed.
    """
    result = text
    for tag in COLOR_MAP:
        result = result.replace(tag, "")
    return result


# ---------------------------------------------------------------------------
# Color upgrade helpers (private)
# ---------------------------------------------------------------------------


def _color256_to_rgb(idx: int) -> tuple[int, int, int]:
    """Convert a 256-color index to an (R, G, B) tuple."""
    if idx < 16:
        return DEFAULT_RGB[idx]
    if idx < 232:
        idx -= 16
        b = idx % 6
        idx //= 6
        g = idx % 6
        r = idx // 6
        levels = (0, 95, 135, 175, 215, 255)
        return (levels[r], levels[g], levels[b])
    gray = 8 + (idx - 232) * 10
    return (gray, gray, gray)


def _palette_to_rgb(palette: list[int]) -> list[tuple[int, int, int]]:
    return [_color256_to_rgb(idx) for idx in palette]


def _map_index(code: int) -> int | None:
    if 30 <= code <= 37:
        return code - 30
    if 90 <= code <= 97:
        return 8 + (code - 90)
    if 40 <= code <= 47:
        return code - 40
    if 100 <= code <= 107:
        return 8 + (code - 100)
    return None


def _convert_sgr_256(match: re.Match, palette: list[int]) -> str:
    seq = match.group(1)
    if seq == "":
        return match.group(0)
    parts = seq.split(";")
    if "38" in parts or "48" in parts:
        return match.group(0)
    new_parts = []
    for p in parts:
        if not p:
            continue
        code = int(p)
        idx = _map_index(code)
        if idx is None:
            new_parts.append(str(code))
            continue
        color = palette[idx]
        if 30 <= code <= 37 or 90 <= code <= 97:
            new_parts.append(f"38;5;{color}")
        else:
            new_parts.append(f"48;5;{color}")
    if not new_parts:
        return match.group(0)
    return f"\x1b[{';'.join(new_parts)}m"


def _convert_tokens_256(text: str, palette: list[int]) -> str:
    def repl(m: re.Match) -> str:
        kind = m.group(1)
        raw = int(m.group(2))
        idx = raw % 16
        color = palette[idx]
        return f"{{{'F' if kind == 'P' else 'B'}{color}}}"

    return _TOKEN_RE.sub(repl, text)


def _convert_sgr_tc(match: re.Match, rgb_palette: list[tuple[int, int, int]]) -> str:
    seq = match.group(1)
    if seq == "":
        return match.group(0)
    parts = seq.split(";")
    if "38" in parts or "48" in parts:
        return match.group(0)
    new_parts = []
    for p in parts:
        if not p:
            continue
        code = int(p)
        idx = _map_index(code)
        if idx is None:
            new_parts.append(str(code))
            continue
        r, g, b = rgb_palette[idx]
        if 30 <= code <= 37 or 90 <= code <= 97:
            new_parts.append(f"38;2;{r};{g};{b}")
        else:
            new_parts.append(f"48;2;{r};{g};{b}")
    if not new_parts:
        return match.group(0)
    return f"\x1b[{';'.join(new_parts)}m"


def _convert_tokens_tc(text: str, rgb_palette: list[tuple[int, int, int]]) -> str:
    def repl(m: re.Match) -> str:
        kind = m.group(1)
        raw = int(m.group(2))
        idx = raw % 16
        r, g, b = rgb_palette[idx]
        if kind == "P":
            return f"\x1b[38;2;{r};{g};{b}m"
        return f"\x1b[48;2;{r};{g};{b}m"

    return _TOKEN_RE.sub(repl, text)


# ---------------------------------------------------------------------------
# Preview helpers (private)
# ---------------------------------------------------------------------------

_PREVIEW_COLOR_MAP = {
    "k": 30,
    "r": 31,
    "g": 32,
    "y": 33,
    "b": 34,
    "m": 35,
    "c": 36,
    "w": 37,
}

_TILDE_MAP: dict[str, tuple[str, str]] = {
    "1": ("+", "g"),
    "2": ("+", "w"),
    "3": ("+", "c"),
    "4": ("+", "r"),
    "5": ("+", "m"),
    "6": ("+", "y"),
    "7": ("-", "w"),
    "0": ("-", "x"),
    "r": ("+", "r"),
    "R": ("+", "r"),
    "g": ("+", "g"),
    "G": ("+", "g"),
    "y": ("+", "y"),
    "Y": ("+", "y"),
    "b": ("+", "b"),
    "B": ("+", "b"),
    "m": ("+", "m"),
    "M": ("+", "m"),
    "c": ("+", "c"),
    "C": ("+", "c"),
    "w": ("+", "w"),
    "W": ("+", "w"),
    "d": ("-", "w"),
    "D": ("-", "w"),
    "E": ("+", "r"),
}


def _emit_color(polarity: str, color_char: str) -> str:
    if color_char == "x":
        return "\x1b[0m"
    code = _PREVIEW_COLOR_MAP.get(color_char)
    if code is None:
        return ""
    if polarity == "+":
        return f"\x1b[0;1;{code}m"
    return f"\x1b[0;{code}m"


def _handle_extended_tokens(text: str) -> str:
    def repl(m: re.Match) -> str:
        kind = m.group(1)
        val = int(m.group(2))
        if kind == "F":
            return f"\x1b[38;5;{val}m"
        if kind == "B":
            return f"\x1b[48;5;{val}m"
        if kind in ("P", "T"):
            idx = val % 16
            bright = idx >= 8
            base = idx % 8
            code = (90 + base if bright else 30 + base) if kind == "P" else (100 + base if bright else 40 + base)
            return f"\x1b[{code}m"

    return _EXT_TOKEN_RE.sub(repl, text)


def _handle_tilde_codes(text: str) -> str:
    out = []
    i = 0
    while i < len(text):
        if text[i] == "~" and i + 1 < len(text):
            code = text[i + 1]
            if code in _TILDE_MAP:
                polarity, color_char = _TILDE_MAP[code]
                seq = _emit_color(polarity, color_char)
                if seq:
                    out.append(seq)
                    i += 2
                    continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _handle_twgs_tokens(text: str) -> str:
    out = []
    i = 0
    while i < len(text):
        if text[i] == "{" and i + 3 < len(text) and text[i + 3] == "}":
            polarity = text[i + 1]
            color_char = text[i + 2]
            if polarity in ("+", "-"):
                seq = _emit_color(polarity, color_char)
                if seq:
                    out.append(seq)
                    i += 4
                    continue
        out.append(text[i])
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Public color upgrade API
# ---------------------------------------------------------------------------


def upgrade_to_256(text: str, palette: list[int] | None = None) -> str:
    """Replace SGR 16-color sequences and {P#}/{T#} tokens with 256-color equivalents.

    Args:
        text: ANSI text possibly containing 16-color SGR codes or BBS palette tokens.
        palette: 16-entry list mapping BBS color indices to 256-color indices.
            Defaults to :data:`DEFAULT_PALETTE`.

    Returns:
        Text with 16-color codes replaced by ``38;5;N`` / ``48;5;N`` equivalents.
    """
    pal = DEFAULT_PALETTE if palette is None else palette
    text = _convert_tokens_256(text, pal)
    return _SGR_RE.sub(lambda m: _convert_sgr_256(m, pal), text)


def upgrade_to_truecolor(text: str, palette: list[int] | None = None) -> str:
    """Replace SGR 16-color sequences and {P#}/{T#} tokens with 24-bit truecolor.

    Args:
        text: ANSI text possibly containing 16-color SGR codes or BBS palette tokens.
        palette: 16-entry list mapping BBS color indices to 256-color indices used to
            derive RGB values.  Defaults to :data:`DEFAULT_PALETTE`.

    Returns:
        Text with 16-color codes replaced by ``38;2;R;G;B`` / ``48;2;R;G;B`` equivalents.
    """
    pal = DEFAULT_PALETTE if palette is None else palette
    rgb_palette = _palette_to_rgb(pal)
    text = _convert_tokens_tc(text, rgb_palette)
    return _SGR_RE.sub(lambda m: _convert_sgr_tc(m, rgb_palette), text)


def preview_ansi(text: str) -> str:
    """Convert all BBS color formats to standard ANSI escape sequences.

    Handles:
    - ``{F###}`` / ``{B###}`` 256-color tokens
    - ``{P#}`` / ``{T#}`` legacy BBS palette tokens
    - ``~N`` tilde codes
    - ``{+c}`` / ``{-x}`` TWGS brace tokens

    Args:
        text: Raw BBS screen text with mixed color tokens.

    Returns:
        Text with all color tokens replaced by standard ANSI escapes.
    """
    text = _handle_extended_tokens(text)
    text = _handle_tilde_codes(text)
    text = _handle_twgs_tokens(text)
    return text
