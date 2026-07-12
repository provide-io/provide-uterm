#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Graphical target registry interfaces."""

from provide.uterm.server.graphical.targets import (
    GraphicalTargetAlreadyExistsError,
    GraphicalTargetClosedError,
    GraphicalTargetError,
    GraphicalTargetForbiddenError,
    GraphicalTargetImmutableError,
    GraphicalTargetNotFoundError,
    GraphicalTargetRegistry,
    GraphicalTargetScope,
    GraphicalTargetTransactionError,
)

__all__ = [
    "GraphicalTargetAlreadyExistsError",
    "GraphicalTargetClosedError",
    "GraphicalTargetError",
    "GraphicalTargetForbiddenError",
    "GraphicalTargetImmutableError",
    "GraphicalTargetNotFoundError",
    "GraphicalTargetRegistry",
    "GraphicalTargetScope",
    "GraphicalTargetTransactionError",
]
