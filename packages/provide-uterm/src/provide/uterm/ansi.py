#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
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

# The built-in dialect handlers live in a sibling module (per-file line budget);
# re-exported here so ``from provide.uterm.ansi import _handle_*`` keeps working
# and the registry below can register them.
from provide.uterm._ansi_dialects import (
    _emit_color as _emit_color,
)
from provide.uterm._ansi_dialects import (
    _handle_brace_tokens as _handle_brace_tokens,
)
from provide.uterm._ansi_dialects import (
    _handle_extended_tokens as _handle_extended_tokens,
)
from provide.uterm._ansi_dialects import (
    _handle_pipe_codes as _handle_pipe_codes,
)
from provide.uterm._ansi_dialects import (
    _handle_tilde_codes as _handle_tilde_codes,
)

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
    39,  # bright blue (was 51 = (0,255,255) cyan — a mislabel; 39 = (0,175,255))
    201,  # bright magenta
    87,  # bright cyan
    231,  # bright white
]

# Direct RGB tuples for the 16 base BBS colors (truecolor output)
# Base red/blue/magenta lifted for WCAG AA (4.5:1) on the #0a0a0a terminal
# background (2026-07-16); bright variants (indices 9/12/13) unchanged.
DEFAULT_RGB: list[tuple[int, int, int]] = [
    (0, 0, 0),  # black
    (235, 77, 77),  # red
    (0, 175, 0),  # green
    (215, 175, 0),  # yellow/brown
    (64, 128, 255),  # blue
    (224, 48, 224),  # magenta
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


def _is_foreground(code: int) -> bool:
    return 30 <= code <= 37 or 90 <= code <= 97


def _brighten_fg_index(idx: int) -> int:
    """A bold foreground selects its BRIGHT palette entry (idx 0-7 -> 8-15).

    Standard DOS/BBS and default xterm (drawBoldTextInBrightColors) semantics:
    ``\\x1b[1;31m`` is bright red, not dim red. Already-bright indices (8-15)
    are left alone so bold on a 90-97 code does not run off the 16-color range.
    """
    return idx + 8 if idx < 8 else idx


def _convert_sgr(
    match: re.Match[str],
    emit_fg: Callable[[int], str],
    emit_bg: Callable[[int], str],
) -> str:
    """Shared SGR 16-color walk; *emit_fg*/*emit_bg* render one palette index.

    The parsing, bold handling and non-color pass-through are identical for the
    256-color and truecolor upgrades — only the final ``38;…`` / ``48;…`` emit
    format differs, which the caller supplies as closures over its palette.
    """
    seq = match.group(1)
    if seq == "":
        return match.group(0)
    parts = seq.split(";")
    if "38" in parts or "48" in parts:
        return match.group(0)
    bold = "1" in parts
    new_parts = []
    for p in parts:
        if not p:
            continue
        code = int(p)
        idx = _map_index(code)
        if idx is None:
            new_parts.append(str(code))
            continue
        fg = _is_foreground(code)
        if fg and bold:
            idx = _brighten_fg_index(idx)
        if fg:
            new_parts.append(emit_fg(idx))
        else:
            new_parts.append(emit_bg(idx))
    if not new_parts:
        return match.group(0)
    return f"\x1b[{';'.join(new_parts)}m"


def _convert_tokens(text: str, emit: Callable[[str, int], str]) -> str:
    """Shared {P#}/{T#} token walk; *emit* renders one (kind, palette index)."""

    def repl(m: re.Match[str]) -> str:
        kind = m.group(1)
        raw = int(m.group(2))
        idx = raw % 16
        return emit(kind, idx)

    return _TOKEN_RE.sub(repl, text)


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

    def emit_token(kind: str, idx: int) -> str:
        return f"{{{'F' if kind == 'P' else 'B'}{pal[idx]}}}"

    def emit_fg(idx: int) -> str:
        return f"38;5;{pal[idx]}"

    def emit_bg(idx: int) -> str:
        return f"48;5;{pal[idx]}"

    text = _convert_tokens(text, emit_token)
    return _SGR_RE.sub(lambda m: _convert_sgr(m, emit_fg, emit_bg), text)


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

    def emit_token(kind: str, idx: int) -> str:
        r, g, b = rgb_palette[idx]
        if kind == "P":
            return f"\x1b[38;2;{r};{g};{b}m"
        return f"\x1b[48;2;{r};{g};{b}m"

    def emit_fg(idx: int) -> str:
        r, g, b = rgb_palette[idx]
        return f"38;2;{r};{g};{b}"

    def emit_bg(idx: int) -> str:
        r, g, b = rgb_palette[idx]
        return f"48;2;{r};{g};{b}"

    text = _convert_tokens(text, emit_token)
    return _SGR_RE.sub(lambda m: _convert_sgr(m, emit_fg, emit_bg), text)


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
