#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Terminal print helpers and output path utilities for demo recording scripts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

BASE_OUT = Path("demo")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_CYAN = "\033[1;36m"
_GREEN = "\033[1;32m"
_YELLOW = "\033[1;33m"
_MAGENTA = "\033[1;35m"
_DIM = "\033[2m"

_ANSI_RE = re.compile(r"\x1b\[[^a-zA-Z]*[a-zA-Z]")


def banner(title: str) -> None:
    """Print a bold magenta section banner."""
    bar = "═" * (len(title) + 4)
    print(f"\n{_MAGENTA}{bar}{_RESET}", flush=True)
    print(f"{_MAGENTA}  {_BOLD}{title}{_RESET}{_MAGENTA}  {_RESET}", flush=True)
    print(f"{_MAGENTA}{bar}{_RESET}\n", flush=True)


def ok(msg: str) -> None:
    """Print a green success line."""
    print(f"{_GREEN}  ✓ {msg}{_RESET}", flush=True)


def info(msg: str) -> None:
    """Print a cyan info line."""
    print(f"{_CYAN}  → {msg}{_RESET}", flush=True)


def warn(msg: str) -> None:
    """Print a yellow warning line."""
    print(f"{_YELLOW}  ! {msg}{_RESET}", flush=True)


def kv(key: str, value: Any) -> None:
    """Print a dim key: bold value pair."""
    print(f"    {_DIM}{key}:{_RESET} {_BOLD}{value}{_RESET}", flush=True)


def out_dir(feature: str, base: Path = BASE_OUT) -> Path:
    """Return demo/recordings/<feature>/, creating it and screenshots/ if absent."""
    d = base / feature
    (d / "screenshots").mkdir(parents=True, exist_ok=True)
    return d


def clean_terminal_output(raw: str) -> str:
    """Strip ANSI escapes and return the last meaningful non-prompt content line.

    Used to extract readable text from PTY output_delta for display in demos.
    Returns the last non-empty, non-prompt line, or '(no output)' if none found.
    """
    clean = _ANSI_RE.sub("", raw)
    lines = [ln.rstrip("\r").strip() for ln in clean.split("\n")]
    meaningful = [ln for ln in lines if ln and not ln.endswith("$") and not ln.endswith("#")]
    if not meaningful:
        return "(no output)"
    last = meaningful[-1]
    last = re.sub(r"^[^$#]*[$#]\s*", "", last)
    return last or "(no output)"
