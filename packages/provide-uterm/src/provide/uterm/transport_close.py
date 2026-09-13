#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""How a transport connection ended.

Every :class:`~provide.uterm.transports.base.ConnectionTransport` reports the end
of its connection as a :class:`TransportClosedError` carrying a
:class:`TransportClose`: which side ended it, plus the protocol's close code and
reason when it has them. :class:`~provide.uterm.transport_session.TransportSession`
keeps the close it observed, so a caller can tell a client-side keepalive
timeout from a server closing the socket.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CloseInitiator(StrEnum):
    """Which side ended a connection."""

    LOCAL = "local"
    REMOTE = "remote"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TransportClose:
    """Why a transport connection ended.

    Attributes:
        initiator: The side that ended the connection.
        code: Protocol close code, when the protocol has one (WebSocket).
        reason: Protocol close reason, when one was given.
        detail: Transport-specific description, such as the underlying error.
    """

    initiator: CloseInitiator
    code: int | None = None
    reason: str = ""
    detail: str = ""

    def summary(self) -> str:
        """One line: the initiator, then code and reason, then any detail."""
        parts = [f"{self.initiator.value} close"]
        if self.code is not None:
            parts.append(str(self.code))
        if self.reason:
            parts.append(self.reason)
        text = " ".join(parts)
        return f"{text} ({self.detail})" if self.detail else text


class TransportClosedError(ConnectionError):
    """Raised by a transport when its connection has ended."""

    def __init__(self, message: str, close: TransportClose) -> None:
        super().__init__(f"{message} ({close.summary()})")
        self.close = close


def close_from_exception(exc: BaseException, initiator: CloseInitiator = CloseInitiator.UNKNOWN) -> TransportClose:
    """Describe a connection that ended with *exc*, attributing it to *initiator*."""
    return TransportClose(initiator, detail=f"{type(exc).__name__}: {exc}")


__all__ = ["CloseInitiator", "TransportClose", "TransportClosedError", "close_from_exception"]
