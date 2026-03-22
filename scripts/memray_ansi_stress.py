#!/usr/bin/env python
"""Memray stress test for ANSI color processing and normalization."""

from provide.terminal.ansi import normalize_colors, upgrade_to_256, upgrade_to_truecolor

# Representative BBS strings covering all color token types
CORPUS = [
    # Extended tokens
    "{F160}Red text{F252}",
    # Tilde codes
    "~1Green ~2White ~4Red ~0Reset",
    # Pipe codes
    "|00Dark |15Bright |20Green background",
    # Brace tokens
    "{+c}Cyan{-x} {+r}Red{-k} {+Bw}White",
    # Raw ANSI
    "\x1b[31mRed\x1b[0m \x1b[1;32mBright Green\x1b[0m",
    # Mixed
    "{F196}Color~1tilde|02pipe{+y}brace\x1b[1m",
    # Plain text
    "No color codes here",
    # Lengthy (1000 chars)
    "Long text " * 100,
]


def main() -> None:
    """Run 500K normalize, 100K upgrade_to_256, 100K upgrade_to_truecolor cycles."""
    # Normalize: 500K cycles
    for _ in range(500_000):
        for s in CORPUS:
            normalize_colors(s)

    # upgrade_to_256: 100K cycles
    for _ in range(100_000):
        for s in CORPUS:
            upgrade_to_256(s)

    # upgrade_to_truecolor: 100K cycles
    for _ in range(100_000):
        for s in CORPUS:
            upgrade_to_truecolor(s)


if __name__ == "__main__":
    main()
