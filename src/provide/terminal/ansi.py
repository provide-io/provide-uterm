#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""ANSI color code conversion for BBS terminal output.

Provides a pluggable dialect registry for converting BBS color tokens to
standard ANSI escape sequences, plus color-upgrade utilities (16-color →
256-color / truecolor).

Built-in dialects: extended tokens ({F#}/{B#}/{P#}/{T#}), TWGS brace tokens
({+c}/{-x}/{+Bw}/{NK}), tilde codes (~N), and pipe codes (|00-|23).
Additional dialects can be registered at runtime via
:func:`register_color_dialect`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

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

# Common ANSI sequences
CLEAR_SCREEN: str = "\033[2J\033[H"
BOLD: str = "\033[1m"
RESET: str = "\033[0m"


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


def _convert_sgr_256(match: re.Match[str], palette: list[int]) -> str:
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
    def repl(m: re.Match[str]) -> str:
        kind = m.group(1)
        raw = int(m.group(2))
        idx = raw % 16
        color = palette[idx]
        return f"{{{'F' if kind == 'P' else 'B'}{color}}}"

    return _TOKEN_RE.sub(repl, text)


def _convert_sgr_tc(match: re.Match[str], rgb_palette: list[tuple[int, int, int]]) -> str:
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
    def repl(m: re.Match[str]) -> str:
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

_BRACE_TOKEN_MAP: dict[str, str] = {
    "{+c}": "\x1b[1;36m",
    "{-c}": "\x1b[0;36m",
    "{+r}": "\x1b[1;31m",
    "{-r}": "\x1b[0;31m",
    "{+g}": "\x1b[1;32m",
    "{-g}": "\x1b[0;32m",
    "{+y}": "\x1b[1;33m",
    "{-y}": "\x1b[0;33m",
    "{+b}": "\x1b[1;34m",
    "{-b}": "\x1b[0;34m",
    "{+m}": "\x1b[1;35m",
    "{-m}": "\x1b[0;35m",
    "{+w}": "\x1b[1;37m",
    "{+Bw}": "\x1b[1;37m",
    "{-w}": "\x1b[0;37m",
    "{+k}": "\x1b[1;30m",
    "{-k}": "\x1b[0;30m",
    "{-x}": "\x1b[0m",
    "{NK}": "\x1b[0m",
    "{T}": "\x1b[1m",
    "{t}": "\x1b[0m",
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
    def repl(m: re.Match[str]) -> str:
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
        return m.group(0)  # pragma: no cover — regex excludes non-FBPT kinds

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
                if seq:  # pragma: no branch
                    out.append(seq)
                    i += 2
                    continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _handle_brace_tokens(text: str) -> str:
    """Convert ``{+c}``/``{-x}`` brace tokens to ANSI escapes.

    Includes the TWGS-specific ``{+Bw}`` header token in addition to the
    single-character color tags.
    """
    out = []
    i = 0
    while i < len(text):
        if text[i] == "{":
            if i + 2 < len(text):
                token = text[i : i + 3]
                if token in _BRACE_TOKEN_MAP:
                    out.append(_BRACE_TOKEN_MAP[token])
                    i += 3
                    continue
            if i + 3 < len(text):
                token = text[i : i + 4]
                if token in _BRACE_TOKEN_MAP:
                    out.append(_BRACE_TOKEN_MAP[token])
                    i += 4
                    continue
            if i + 4 < len(text):
                token = text[i : i + 5]
                if token in _BRACE_TOKEN_MAP:
                    out.append(_BRACE_TOKEN_MAP[token])
                    i += 5
                    continue
        out.append(text[i])
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Pipe codes (|00-|23) — most common BBS color format
# ---------------------------------------------------------------------------

_PIPE_RE = re.compile(r"\|(\d{2})")

# DOS color order → ANSI SGR codes
_DOS_TO_ANSI_FG = [30, 34, 32, 36, 31, 35, 33, 37]  # |00-|07 (dim)
_DOS_TO_ANSI_BG = [40, 44, 42, 46, 41, 45, 43, 47]  # |16-|23


def _handle_pipe_codes(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        code = int(m.group(1))
        if code <= 7:
            return f"\x1b[{_DOS_TO_ANSI_FG[code]}m"
        if code <= 15:
            return f"\x1b[{_DOS_TO_ANSI_FG[code - 8] + 60}m"
        if code <= 23:
            return f"\x1b[{_DOS_TO_ANSI_BG[code - 16]}m"
        return m.group(0)

    return _PIPE_RE.sub(repl, text)


# ---------------------------------------------------------------------------
# Dialect registry
# ---------------------------------------------------------------------------

_dialect_registry: list[tuple[str, Callable[[str], str]]] = []


def register_color_dialect(name: str, handler: Callable[[str], str]) -> None:
    """Register a color token dialect handler.

    Handlers are called in registration order by :func:`normalize_colors`.

    Args:
        name: Unique name for the dialect (e.g. ``"pipe_codes"``).
        handler: A ``str → str`` function that converts tokens to ANSI escapes.

    Raises:
        ValueError: If *name* is already registered.
    """
    for existing_name, _ in _dialect_registry:
        if existing_name == name:
            msg = f"color dialect {name!r} is already registered"
            raise ValueError(msg)
    _dialect_registry.append((name, handler))


def unregister_color_dialect(name: str) -> None:
    """Remove a previously registered dialect.

    Raises:
        KeyError: If *name* is not registered.
    """
    for i, (existing_name, _) in enumerate(_dialect_registry):
        if existing_name == name:
            _dialect_registry.pop(i)
            return
    msg = f"color dialect {name!r} is not registered"
    raise KeyError(msg)


def registered_dialects() -> list[str]:
    """Return the names of all registered dialects, in call order."""
    return [name for name, _ in _dialect_registry]


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


def normalize_colors(text: str) -> str:
    """Convert all registered BBS color token formats to standard ANSI escapes.

    Runs each registered dialect handler in order.  Built-in dialects handle:

    - ``{F###}`` / ``{B###}`` 256-color tokens
    - ``{P#}`` / ``{T#}`` legacy BBS palette tokens
    - ``~N`` tilde codes
    - ``|00``-``|23`` pipe codes

    Additional dialects can be added via :func:`register_color_dialect`.

    Args:
        text: Raw BBS screen text with mixed color tokens.

    Returns:
        Text with all color tokens replaced by standard ANSI escapes.
    """
    for _name, handler in _dialect_registry:
        text = handler(text)
    return text


preview_ansi = normalize_colors


# ---------------------------------------------------------------------------
# Register built-in dialects
# ---------------------------------------------------------------------------

register_color_dialect("brace_tokens", _handle_brace_tokens)
register_color_dialect("extended_tokens", _handle_extended_tokens)
register_color_dialect("tilde_codes", _handle_tilde_codes)
register_color_dialect("pipe_codes", _handle_pipe_codes)
