#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Core bridge primitives for terminal takeover and coordination.

This package is owned by ``provide-uterm`` and intentionally contains only
transport-independent primitives. Server runtime modules live under
``provide.uterm.server.bridge``.
"""

from provide.uterm.bridge.base import HijackableMixin
from provide.uterm.bridge.coordinator import (
    AcquireResult,
    HijackCoordinator,
    HijackSession,
)

__all__ = ["AcquireResult", "HijackCoordinator", "HijackSession", "HijackableMixin"]
