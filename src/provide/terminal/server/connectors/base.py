#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Connector abstraction for hosted terminal sessions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class SessionConnector(ABC):
    """Abstract connector used by HostedSessionRuntime."""

    @abstractmethod
    async def start(self) -> None:
        """Start the upstream session."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the upstream session."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if the connector is live."""

    @abstractmethod
    async def poll_messages(self) -> list[dict[str, Any]]:
        """Return any spontaneous worker-protocol messages to emit."""

    @abstractmethod
    async def handle_input(self, data: str) -> list[dict[str, Any]]:
        """Process user input and return worker-protocol messages."""

    @abstractmethod
    async def handle_control(self, action: str) -> list[dict[str, Any]]:
        """Process a control action and return worker-protocol messages."""

    @abstractmethod
    async def get_snapshot(self) -> dict[str, Any]:
        """Return a worker-protocol snapshot message."""

    @abstractmethod
    async def get_analysis(self) -> str:
        """Return a human-readable analysis string."""

    @abstractmethod
    async def set_mode(self, mode: str) -> list[dict[str, Any]]:
        """Apply an input mode change and return worker-protocol messages."""

    @abstractmethod
    async def clear(self) -> list[dict[str, Any]]:
        """Clear/reset the session state."""
