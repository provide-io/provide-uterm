#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations


class ControlPlaneError(Exception):
    """Base error for control-plane bootstrap and transaction failures."""


class ControlPlaneConfigurationError(ControlPlaneError):
    """Raised when control-plane configuration is invalid or incomplete."""


class ControlPlaneCapabilityError(ControlPlaneError):
    """Raised when a caller requests a capability the engine does not expose."""


class ControlPlaneConflictError(ControlPlaneError):
    """Raised on commit when a write conflicts with a concurrently committed transaction.

    Mirrors the serialization failure the SQLite backend produces via
    ``BEGIN IMMEDIATE`` + a held transaction lock: two overlapping
    transactions that write the same key cannot both succeed. The memory
    backend detects this optimistically at commit time so that, e.g., a
    lease-acquire race yields exactly one winner on both backends.
    """
