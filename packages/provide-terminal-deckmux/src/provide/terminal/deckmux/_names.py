#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""DeckMux identity — deterministic name and color generation."""

from __future__ import annotations

import hashlib

_ADJECTIVES = [
    "red",
    "blue",
    "green",
    "amber",
    "silver",
    "coral",
    "jade",
    "onyx",
    "pearl",
    "ruby",
    "gold",
    "iron",
    "copper",
    "bronze",
    "crystal",
    "storm",
    "frost",
    "ember",
    "dusk",
    "dawn",
    "ash",
    "moss",
    "slate",
    "flint",
    "cedar",
    "birch",
    "maple",
    "sage",
    "thorn",
    "drift",
    "spark",
    "blaze",
]

_ANIMALS = [
    "fox",
    "hawk",
    "wolf",
    "otter",
    "lynx",
    "crane",
    "bear",
    "deer",
    "eagle",
    "raven",
    "heron",
    "viper",
    "shark",
    "whale",
    "tiger",
    "panther",
    "falcon",
    "condor",
    "bison",
    "moose",
    "cobra",
    "gecko",
    "puma",
    "osprey",
    "badger",
    "ferret",
    "marten",
    "jackal",
    "ibis",
    "newt",
    "pike",
    "wren",
]

_COLORS = [
    "#e74c3c",
    "#3498db",
    "#2ecc71",
    "#9b59b6",
    "#e67e22",
    "#1abc9c",
    "#f39c12",
    "#e91e63",
    "#00bcd4",
    "#8bc34a",
    "#ff5722",
    "#607d8b",
]


def _hash_int(value: str) -> int:
    """Deterministic hash of a string to an integer."""
    return int(hashlib.sha256(value.encode()).hexdigest(), 16)


def generate_name(connection_id: str) -> str:
    """Generate a deterministic display name from a connection ID."""
    h = _hash_int(connection_id)
    adj = _ADJECTIVES[h % len(_ADJECTIVES)]
    animal = _ANIMALS[(h >> 8) % len(_ANIMALS)]
    return f"{adj.title()} {animal.title()}"


def generate_color(connection_id: str, taken: frozenset[str] = frozenset()) -> str:
    """Generate a deterministic color, avoiding already-taken colors."""
    h = _hash_int(connection_id)
    for offset in range(len(_COLORS)):
        color = _COLORS[(h + offset) % len(_COLORS)]
        if color not in taken:
            return color
    return _COLORS[h % len(_COLORS)]  # fallback if all taken


def generate_initials(name: str) -> str:
    """Generate 2-character initials from a display name."""
    parts = name.split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return name[:2].upper()
