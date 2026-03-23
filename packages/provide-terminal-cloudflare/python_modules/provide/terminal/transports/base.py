#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Abstract base class for provide-terminal transport protocols."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ConnectionTransport(ABC):
    """Abstract base for transport protocols (telnet, SSH, chaos wrapper, etc).

    All concrete transports implement this interface so they are
    interchangeable by callers.
    """

    @abstractmethod
    async def connect(self, host: str, port: int, **kwargs: Any) -> None:
        """Establish connection to remote host.

        Args:
            host: Remote hostname or IP address.
            port: Remote port number.
            **kwargs: Protocol-specific connection options.

        Raises:
            ConnectionError: If connection fails.
            asyncio.TimeoutError: If connection times out.
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection and cleanup resources.

        Idempotent — safe to call multiple times.
        """

    @abstractmethod
    async def send(self, data: bytes) -> None:
        """Send raw bytes with proper protocol encoding/escaping.

        Args:
            data: Raw bytes to send.

        Raises:
            ConnectionError: If not connected or send fails.
        """

    @abstractmethod
    async def receive(self, max_bytes: int, timeout_ms: int) -> bytes:
        """Receive raw bytes from the connection.

        Args:
            max_bytes: Maximum bytes to read.
            timeout_ms: Read timeout in milliseconds.

        Returns:
            Bytes read from connection (may be empty on timeout).

        Raises:
            ConnectionError: If not connected or connection lost.
        """

    @abstractmethod
    def is_connected(self) -> bool:
        """Check if connection is active.

        Returns:
            True if connected, False otherwise.
        """
