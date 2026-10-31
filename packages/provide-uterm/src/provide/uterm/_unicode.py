#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The Unicode data uterm's screens are drawn with, pinned to one version.

pyte composes a combining mark into the cell before it with the interpreter's
``unicodedata`` (its combining class, then NFC), and CPython's Unicode tables
move with the interpreter: 14.0 on 3.11, 15.0 on 3.12, 15.1 on 3.13 and 16.0 on
3.14. The same stream then left different text on the screen under different
Pythons, and none of them matched the tables the Go, C# and TypeScript ports
draw with. pyte reads ``unicodedata`` as a module global of ``pyte.screens``,
so importing this module points that global at ``unicodedata2``, a pinned copy
of the Unicode database (the version is the package version, pinned by
uv.lock; 18.0, the version wcwidth's own width tables are). Every pyte screen
in the process composes with it. The cell widths need no such step: wcwidth
ships its own tables, and its version is pinned the same way.

The ports' tables are generated from the same two packages by
scripts/gen_unicode_tables.py.
"""

from __future__ import annotations

import pyte.screens
import unicodedata2  # ty:ignore[unresolved-import]  # a C extension with no stubs

#: The Unicode version combining marks are composed with.
UNICODE_VERSION: str = unicodedata2.unidata_version

if not hasattr(pyte.screens, "unicodedata"):  # pragma: no cover - guards a pyte upgrade
    # Deliberately loud. A pyte that stopped reading a module-level unicodedata
    # would silently compose with the interpreter's tables again.
    raise ImportError("pyte.screens no longer reads a module-level unicodedata; uterm cannot pin its Unicode version")
pyte.screens.unicodedata = unicodedata2
