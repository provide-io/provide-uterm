#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Color-downgrade utilities: truecolor SGR -> 256-color / 16-color.

These are the downgrade counterparts to ``upgrade_to_256`` /
``upgrade_to_truecolor`` in :mod:`provide.terminal.ansi`. Organized as
a subpackage so each concern lives in its own tight file:

- :mod:`.rgb`     — RGB-to-palette-index mapping (``rgb_to_256``, ``rgb_to_16_index``).
- :mod:`.sgr`     — SGR parameter-list rewriting.
- :mod:`.downgrade` — text-level ``downgrade_to_256`` / ``downgrade_to_16``.
- :mod:`.mode`    — unified ``apply_color_mode`` dispatcher (str | bytes).

Consumers should import from the subpackage root:

    from provide.terminal.colors import apply_color_mode, rgb_to_256

or from the package root:

    from provide.terminal import apply_color_mode
"""

from __future__ import annotations

from provide.terminal.colors.downgrade import downgrade_to_16, downgrade_to_256
from provide.terminal.colors.mode import apply_color_mode
from provide.terminal.colors.rgb import rgb_to_16_index, rgb_to_256

__all__ = [
    "apply_color_mode",
    "downgrade_to_16",
    "downgrade_to_256",
    "rgb_to_16_index",
    "rgb_to_256",
]
