from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EngineCapabilities:
    """Engine feature flags discovered at bootstrap time."""

    supports_transactions: bool = True
    supports_migrations: bool = True
    supports_retries: bool = True
