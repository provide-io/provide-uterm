#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Built-in BBS color-token dialect handlers for :mod:`provide.uterm.ansi`.

Self-contained ``str -> str`` converters for the extended-token, tilde-code,
TWGS brace-token, and pipe-code dialects, plus their lookup tables. Split out
of ``ansi.py`` to keep each module within the per-file line budget; the public
surface (``normalize_colors`` + the dialect registry) stays in ``ansi.py``,
which registers these handlers at import time.
"""

from __future__ import annotations

import re

_EXT_TOKEN_RE = re.compile(r"\{([FBPT])(\d{1,3})\}")

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


# Pre-build extended token lookups to eliminate per-match f-string creation.
_EXT_F_LOOKUP = tuple(f"\x1b[38;5;{i}m" for i in range(256))
_EXT_B_LOOKUP = tuple(f"\x1b[48;5;{i}m" for i in range(256))
_EXT_P_LOOKUP = tuple(f"\x1b[{(90 + (i % 8)) if (i % 16) >= 8 else (30 + (i % 8))}m" for i in range(16))
_EXT_T_LOOKUP = tuple(f"\x1b[{(100 + (i % 8)) if (i % 16) >= 8 else (40 + (i % 8))}m" for i in range(16))


def _handle_extended_tokens(text: str) -> str:
    """Convert {F###}/{B###}/{P#}/{T#} extended color tokens to ANSI escapes."""
    parts: list[str] = []
    last_end = 0
    for m in _EXT_TOKEN_RE.finditer(text):
        parts.append(text[last_end : m.start()])
        kind = m.group(1)
        val = int(m.group(2))
        if kind == "F":
            parts.append(_EXT_F_LOOKUP[val] if val < 256 else f"\x1b[38;5;{val}m")
        elif kind == "B":
            parts.append(_EXT_B_LOOKUP[val] if val < 256 else f"\x1b[48;5;{val}m")
        elif kind == "P":
            parts.append(_EXT_P_LOOKUP[val % 16])
        elif kind == "T":  # pragma: no cover — truecolor reserved for future use
            parts.append(_EXT_T_LOOKUP[val % 16])
        last_end = m.end()
    if last_end == 0:
        return text  # no matches
    parts.append(text[last_end:])
    return "".join(parts)


_TILDE_RE = re.compile(r"~(.)")


# Pre-build tilde code → ANSI escape lookup to avoid per-match _emit_color calls.
_TILDE_LOOKUP: dict[str, str] = {}
for _tc, (_pol, _cc) in _TILDE_MAP.items():
    _seq = _emit_color(_pol, _cc)
    if _seq:  # pragma: no cover — _seq always truthy for all defined tilde codes
        _TILDE_LOOKUP[_tc] = _seq


def _handle_tilde_codes(text: str) -> str:
    parts = _TILDE_RE.split(text)
    if len(parts) == 1:
        return text
    for i in range(1, len(parts), 2):
        parts[i] = _TILDE_LOOKUP.get(parts[i], f"~{parts[i]}")
    return "".join(parts)


_BRACE_3_RE = re.compile(r"(\{[+\-][a-zA-Z]\}|\{NK\}|\{T\}|\{t\})")
_BRACE_4_RE = re.compile(r"(\{[+\-]Bw\})")


def _handle_brace_tokens(text: str) -> str:
    """Convert ``{+c}``/``{-x}`` brace tokens to ANSI escapes.

    Includes the TWGS-specific ``{+Bw}`` header token in addition to the
    single-character color tags.
    """
    # Handle 5-char tokens first (longest match first to avoid conflicts)
    parts4 = _BRACE_4_RE.split(text)
    if len(parts4) > 1:
        for i in range(1, len(parts4), 2):
            parts4[i] = _BRACE_TOKEN_MAP.get(parts4[i], parts4[i])
        text = "".join(parts4)
    # Then 3-char and other tokens
    parts3 = _BRACE_3_RE.split(text)
    if len(parts3) > 1:
        for i in range(1, len(parts3), 2):
            parts3[i] = _BRACE_TOKEN_MAP.get(parts3[i], parts3[i])
        return "".join(parts3)
    return text


# ---------------------------------------------------------------------------
# Pipe codes (|00-|23) — most common BBS color format
# ---------------------------------------------------------------------------

_PIPE_RE = re.compile(r"\|(\d{2})")

# DOS color order → ANSI SGR codes
_DOS_TO_ANSI_FG = [30, 34, 32, 36, 31, 35, 33, 37]  # |00-|07 (dim)
_DOS_TO_ANSI_BG = [40, 44, 42, 46, 41, 45, 43, 47]  # |16-|23


_PIPE_LOOKUP: dict[str, str] = {}
for _i in range(24):
    _key = f"{_i:02d}"
    if _i <= 7:
        _PIPE_LOOKUP[_key] = f"\x1b[{_DOS_TO_ANSI_FG[_i]}m"
    elif _i <= 15:
        _PIPE_LOOKUP[_key] = f"\x1b[{_DOS_TO_ANSI_FG[_i - 8] + 60}m"
    else:
        _PIPE_LOOKUP[_key] = f"\x1b[{_DOS_TO_ANSI_BG[_i - 16]}m"


def _handle_pipe_codes(text: str) -> str:
    # split() with a capturing group keeps the matched group in the result list,
    # alternating: [text, match, text, match, ...]. We replace matches in-place
    # via the lookup, avoiding re.sub's per-match string rebuild.
    parts = _PIPE_RE.split(text)
    if len(parts) == 1:
        return text  # no matches — return original without allocation
    for i in range(1, len(parts), 2):
        parts[i] = _PIPE_LOOKUP.get(parts[i], f"|{parts[i]}")
    return "".join(parts)
