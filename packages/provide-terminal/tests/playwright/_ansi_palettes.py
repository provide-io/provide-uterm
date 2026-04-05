#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""ANSI escape sequence generators for E2E color and art tests.

Pure data generators — no test framework or server dependencies.
Each function returns a string of ANSI-escaped text suitable for
writing to an xterm.js terminal via WebSocket.
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# 16-color
# ---------------------------------------------------------------------------

_16_NAMES = [
    "black",
    "red",
    "green",
    "yellow",
    "blue",
    "magenta",
    "cyan",
    "white",
    "bright black",
    "bright red",
    "bright green",
    "bright yellow",
    "bright blue",
    "bright magenta",
    "bright cyan",
    "bright white",
]


def build_16_color_palette() -> str:
    """Build a 16-color palette using standard SGR codes (30-37, 40-47, 90-97, 100-107)."""
    lines: list[str] = []
    lines.append("\x1b[1;37m 16-Color Palette (Standard SGR)\x1b[0m\r\n\r\n")

    lines.append(" Foreground:\r\n")
    row = " "
    for code in range(30, 38):
        row += f"\x1b[{code}m {_16_NAMES[code - 30]:>7s} \x1b[0m"
    lines.append(f"{row}\r\n")
    row = " "
    for code in range(90, 98):
        row += f"\x1b[{code}m {_16_NAMES[code - 82]:>7s} \x1b[0m"
    lines.append(f"{row}\r\n\r\n")

    lines.append(" Background:\r\n")
    row = " "
    for code in range(40, 48):
        row += f"\x1b[{code};37m {_16_NAMES[code - 40]:>7s} \x1b[0m"
    lines.append(f"{row}\r\n")
    row = " "
    for code in range(100, 108):
        row += f"\x1b[{code};30m {_16_NAMES[code - 92]:>7s} \x1b[0m"
    lines.append(f"{row}\r\n\r\n")

    lines.append(" Attributes:\r\n")
    lines.append(" \x1b[1mBold\x1b[0m  \x1b[4mUnderline\x1b[0m  \x1b[5mBlink\x1b[0m  \x1b[7mReverse\x1b[0m")
    lines.append("  \x1b[1;31mBold Red\x1b[0m  \x1b[4;32mUnder Green\x1b[0m  \x1b[7;34mRev Blue\x1b[0m\r\n")

    return "".join(lines)


# ---------------------------------------------------------------------------
# 256-color
# ---------------------------------------------------------------------------


def build_256_color_palette() -> str:
    """Build a 256-color palette using SGR 38;5;N (fg) and 48;5;N (bg) codes."""
    lines: list[str] = []
    lines.append("\x1b[1;37m 256-Color Palette (ESC[48;5;Nm)\x1b[0m\r\n\r\n")

    row = ""
    for n in range(8):
        row += f"\x1b[48;5;{n}m  \x1b[0m"
    lines.append(f" {row}  standard 0-7\r\n")

    row = ""
    for n in range(8, 16):
        row += f"\x1b[48;5;{n}m  \x1b[0m"
    lines.append(f" {row}  bright 8-15\r\n\r\n")

    for block in range(6):
        row = ""
        for i in range(36):
            n = 16 + block * 36 + i
            row += f"\x1b[48;5;{n}m \x1b[0m"
        lines.append(f" {row}\r\n")
    lines.append("\r\n")

    row = ""
    for n in range(232, 256):
        row += f"\x1b[48;5;{n}m  \x1b[0m"
    lines.append(f" {row}  grayscale\r\n\r\n")

    lines.append(" \x1b[1;37mForeground text (ESC[38;5;Nm):\x1b[0m\r\n")
    lines.extend(
        f" \x1b[38;5;{n}m Color {n:>3d}: The quick brown fox \x1b[0m\r\n" for n in [196, 208, 220, 46, 51, 21, 93, 201]
    )

    return "".join(lines)


# ---------------------------------------------------------------------------
# Truecolor
# ---------------------------------------------------------------------------


def build_truecolor_palette() -> str:
    """Build a truecolor gradient using SGR 48;2;R;G;B (background) codes."""
    lines: list[str] = []
    width = 64

    lines.append("\r\n\x1b[1;37m Truecolor Palette (ESC[48;2;R;G;Bm)\x1b[0m\r\n\r\n")

    for label, rfn, gfn, bfn in [
        ("red", lambda i, w: int(i * 255 / (w - 1)), lambda _i, _w: 0, lambda _i, _w: 0),
        ("green", lambda _i, _w: 0, lambda i, w: int(i * 255 / (w - 1)), lambda _i, _w: 0),
        ("blue", lambda _i, _w: 0, lambda _i, _w: 0, lambda i, w: int(i * 255 / (w - 1))),
    ]:
        row = ""
        for i in range(width):
            row += f"\x1b[48;2;{rfn(i, width)};{gfn(i, width)};{bfn(i, width)}m \x1b[0m"
        lines.append(f" {row} {label}\r\n")

    # Rainbow (hue sweep)
    row = ""
    for i in range(width):
        h = i / width * 6
        c = 255
        x = int(255 * (1 - abs(h % 2 - 1)))
        if h < 1:
            r, g, b = c, x, 0
        elif h < 2:
            r, g, b = x, c, 0
        elif h < 3:
            r, g, b = 0, c, x
        elif h < 4:
            r, g, b = 0, x, c
        elif h < 5:
            r, g, b = x, 0, c
        else:
            r, g, b = c, 0, x
        row += f"\x1b[48;2;{r};{g};{b}m \x1b[0m"
    lines.append(f" {row} rainbow\r\n")

    # Grayscale
    row = ""
    for i in range(width):
        v = int(i * 255 / (width - 1))
        row += f"\x1b[48;2;{v};{v};{v}m \x1b[0m"
    lines.append(f" {row} grayscale\r\n")

    return "".join(lines)


# ---------------------------------------------------------------------------
# ANSI Art
# ---------------------------------------------------------------------------


def build_ansi_art() -> str:
    """Build ANSI art showcasing block chars, box drawing, truecolor art, and braille."""
    lines: list[str] = []
    lines.append("\x1b[1;37m ANSI Art Showcase\x1b[0m\r\n\r\n")

    # Block shading
    lines.append(" \x1b[1;36mBlock Shading (░▒▓█):\x1b[0m\r\n")
    blocks = "░▒▓█"
    for fg_code in [31, 32, 33, 34, 35, 36]:
        row = " "
        for _ in range(3):
            for ch in blocks:
                row += f"\x1b[{fg_code}m{ch}\x1b[0m"
        row += f"  \x1b[1;{fg_code}m"
        for _ in range(3):
            for ch in blocks:
                row += ch
        row += "\x1b[0m"
        lines.append(f"{row}\r\n")
    lines.append("\r\n")

    # Box drawing
    lines.append(" \x1b[1;33mBox Drawing:\x1b[0m\r\n")
    title = " provide-terminal "
    w = 40
    pad = w - 2 - len(title)
    lines.append(f"  \x1b[36m╔{'═' * (pad // 2)}{title}{'═' * (pad - pad // 2)}╗\x1b[0m\r\n")
    lines.append(
        "  \x1b[36m║\x1b[0m \x1b[32m✓\x1b[0m 16-color    \x1b[36m│\x1b[0m \x1b[32m✓\x1b[0m 256-color   \x1b[36m║\x1b[0m\r\n"
    )
    lines.append(
        "  \x1b[36m║\x1b[0m \x1b[32m✓\x1b[0m truecolor   \x1b[36m│\x1b[0m \x1b[32m✓\x1b[0m ANSI art    \x1b[36m║\x1b[0m\r\n"
    )
    lines.append(f"  \x1b[36m╠{'═' * (w - 2)}╣\x1b[0m\r\n")
    lines.append("  \x1b[36m║\x1b[0m \x1b[1;37mWorker → Hub → WS → xterm.js  \x1b[32m✓\x1b[0m \x1b[36m║\x1b[0m\r\n")
    lines.append(f"  \x1b[36m╚{'═' * (w - 2)}╝\x1b[0m\r\n\r\n")

    # Truecolor flame
    lines.append(" \x1b[1;31mTruecolor Flame (▄ half-blocks):\x1b[0m\r\n")
    flame_w, flame_h = 60, 8
    for y in range(flame_h):
        row = " "
        for x in range(flame_w):
            cx = abs(x - flame_w / 2) / (flame_w / 2)
            cy = (flame_h - y) / flame_h
            heat = min(1.0, max(0.0, cy * (1 - cx * 0.8) + math.sin(x * 0.7 + y * 0.5) * 0.15))
            r2, g2, b2 = _heat_to_rgb(heat)
            row += f"\x1b[48;2;{r2};{g2};{b2}m \x1b[0m"
        lines.append(f"{row}\r\n")
    lines.append("\r\n")

    # Landscape
    _append_landscape(lines, 60)

    # Braille sine wave
    _append_braille_wave(lines, 60, 4)

    # 256-color plasma
    lines.append(" \x1b[1;37mPlasma (256-color blocks):\x1b[0m\r\n")
    for y in range(6):
        row = " "
        for x in range(40):
            val = (
                math.sin(x * 0.3)
                + math.sin(y * 0.5)
                + math.sin((x + y) * 0.2)
                + math.sin(math.sqrt(x * x + y * y) * 0.3)
            )
            n = max(16, min(231, 16 + int((val + 4) / 8 * 215)))
            row += f"\x1b[48;5;{n}m \x1b[0m"
        lines.append(f"{row}\r\n")

    return "".join(lines)


# ---------------------------------------------------------------------------
# Animation frames
# ---------------------------------------------------------------------------


def build_animation_frames(num_frames: int = 30) -> list[str]:
    """Build a sequence of animated ANSI frames for video capture.

    Returns a list of strings, each a complete frame (cursor-home + content).
    Includes: rotating plasma, bouncing color bar, and cycling gradient.
    """
    frames: list[str] = []
    w, h = 60, 20

    for f in range(num_frames):
        t = f / num_frames * 2 * math.pi
        parts: list[str] = ["\x1b[H"]  # cursor home

        # Title
        hue = f / num_frames
        r, g, b = _hsv_to_rgb(hue, 1.0, 1.0)
        parts.append(f"\x1b[38;2;{r};{g};{b}m\x1b[1m ANSI Animation — frame {f + 1}/{num_frames}\x1b[0m\r\n\r\n")

        # Animated plasma (truecolor bg)
        for y in range(h - 8):
            row = " "
            for x in range(w):
                val = (
                    math.sin(x * 0.1 + t)
                    + math.sin(y * 0.15 - t * 0.7)
                    + math.sin((x + y) * 0.1 + t * 0.5)
                    + math.sin(math.sqrt((x - w / 2) ** 2 + (y - 6) ** 2) * 0.15 - t)
                )
                # Map [-4,4] to hue
                hue2 = (val + 4) / 8
                r2, g2, b2 = _hsv_to_rgb(hue2 % 1.0, 0.8, 0.9)
                row += f"\x1b[48;2;{r2};{g2};{b2}m \x1b[0m"
            parts.append(f"{row}\r\n")

        # Bouncing color bar (256-color)
        bar_pos = int((math.sin(t) + 1) / 2 * (w - 20))
        row = " "
        for x in range(w):
            if bar_pos <= x < bar_pos + 20:
                n = 16 + int((x - bar_pos) / 20 * 215)
                row += f"\x1b[48;5;{n}m \x1b[0m"
            else:
                row += " "
        parts.append(f"{row}\r\n")

        # Scrolling text with cycling colors
        offset = f * 2
        msg = "  ★ provide-terminal — 16/256/truecolor proven end-to-end ★  "
        row = " "
        for i in range(w):
            ch = msg[(i + offset) % len(msg)]
            ci = (i + f * 3) % 256
            row += f"\x1b[38;5;{ci}m{ch}\x1b[0m"
        parts.append(f"{row}\r\n")

        # Spinning braille wheel
        braille_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        spinner = braille_chars[f % len(braille_chars)]
        r3, g3, b3 = _hsv_to_rgb((f / num_frames) % 1.0, 1.0, 1.0)
        parts.append(f"\r\n \x1b[38;2;{r3};{g3};{b3}m{spinner}\x1b[0m Processing...")

        frames.append("".join(parts))

    return frames


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    """Convert HSV (0-1 range) to RGB (0-255)."""
    if s == 0:
        c = int(v * 255)
        return c, c, c
    i = int(h * 6)
    f = h * 6 - i
    p, q, t = int(v * (1 - s) * 255), int(v * (1 - s * f) * 255), int(v * (1 - s * (1 - f)) * 255)
    vi = int(v * 255)
    match i % 6:
        case 0:
            return vi, t, p
        case 1:
            return q, vi, p
        case 2:
            return p, vi, t
        case 3:
            return p, q, vi
        case 4:
            return t, p, vi
        case _:
            return vi, p, q


def _heat_to_rgb(heat: float) -> tuple[int, int, int]:
    """Map heat 0-1 → flame color (black→red→orange→yellow→white)."""
    if heat < 0.25:
        return int(heat * 4 * 200), 0, 0
    if heat < 0.5:
        return 200 + int((heat - 0.25) * 4 * 55), int((heat - 0.25) * 4 * 130), 0
    if heat < 0.75:
        return 255, 130 + int((heat - 0.5) * 4 * 125), int((heat - 0.5) * 4 * 60)
    return 255, 255, 60 + int((heat - 0.75) * 4 * 195)


def _append_landscape(lines: list[str], land_w: int) -> None:
    """Append a truecolor landscape (sky + mountains + terrain)."""
    lines.append(" \x1b[1;34mTruecolor Landscape:\x1b[0m\r\n")
    for y in range(4):
        row = " "
        t = y / 3
        for _x in range(land_w):
            r2 = int(20 + t * 100)
            g2 = int(30 + t * 140)
            b2 = int(80 + t * 175)
            row += f"\x1b[48;2;{r2};{g2};{b2}m \x1b[0m"
        lines.append(f"{row}\r\n")
    mountain = [0.3, 0.5, 0.7, 0.9, 1.0, 0.9, 0.7, 0.8, 0.95, 1.0, 0.85, 0.6, 0.4, 0.3, 0.5]
    for y in range(3):
        row = " "
        for x in range(land_w):
            mi = int(x / land_w * (len(mountain) - 1))
            mh = mountain[mi]
            if y < int((1 - mh) * 3):
                row += "\x1b[48;2;120;170;255m \x1b[0m"
            else:
                g2 = 80 + int(math.sin(x * 0.3) * 40) + y * 20
                row += f"\x1b[48;2;30;{min(g2, 200)};20m \x1b[0m"
        lines.append(f"{row}\r\n")
    lines.append("\r\n")


def _append_braille_wave(lines: list[str], bw: int, bh: int) -> None:
    """Append a braille sine wave pattern."""
    lines.append(" \x1b[1;35mBraille Sine Wave:\x1b[0m\r\n")
    dots: list[list[bool]] = [[False] * (bw * 2) for _ in range(bh * 4)]
    for px in range(bw * 2):
        for freq, _idx in [(0.15, 0), (0.22, 1)]:
            val = math.sin(px * freq) * 0.4 + 0.5
            py = int(val * (bh * 4 - 1))
            if 0 <= py < bh * 4:
                dots[py][px] = True
    for cy in range(bh):
        row = " \x1b[38;2;100;200;255m"
        for cx in range(bw):
            code = 0x2800
            for dy in range(4):
                for dx in range(2):
                    py2 = cy * 4 + dy
                    px2 = cx * 2 + dx
                    if py2 < len(dots) and px2 < len(dots[0]) and dots[py2][px2]:
                        if dx == 0:
                            code += [0x01, 0x02, 0x04, 0x40][dy]
                        else:
                            code += [0x08, 0x10, 0x20, 0x80][dy]
            row += chr(code)
        row += "\x1b[0m"
        lines.append(f"{row}\r\n")
    lines.append("\r\n")
