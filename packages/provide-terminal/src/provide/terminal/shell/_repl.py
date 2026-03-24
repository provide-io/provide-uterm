#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shim: re-exports from provide.shell._repl."""

from provide.shell._repl import LineBuffer  # type: ignore[import-not-found]

__all__ = ["LineBuffer"]
