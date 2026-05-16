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
