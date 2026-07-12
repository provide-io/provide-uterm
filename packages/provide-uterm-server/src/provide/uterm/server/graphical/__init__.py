#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Graphical target registry interfaces."""

from provide.uterm.server.graphical.targets import (
    GraphicalTargetAlreadyExistsError,
    GraphicalTargetError,
    GraphicalTargetImmutableError,
    GraphicalTargetNotFoundError,
    GraphicalTargetRegistry,
    GraphicalTargetTransactionError,
)

__all__ = [
    "GraphicalTargetAlreadyExistsError",
    "GraphicalTargetError",
    "GraphicalTargetImmutableError",
    "GraphicalTargetNotFoundError",
    "GraphicalTargetRegistry",
    "GraphicalTargetTransactionError",
]
