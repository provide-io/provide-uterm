from __future__ import annotations


class ControlPlaneError(Exception):
    """Base error for control-plane bootstrap and transaction failures."""


class ControlPlaneConfigurationError(ControlPlaneError):
    """Raised when control-plane configuration is invalid or incomplete."""


class ControlPlaneCapabilityError(ControlPlaneError):
    """Raised when a caller requests a capability the engine does not expose."""
