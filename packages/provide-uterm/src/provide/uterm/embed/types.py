#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Embed types, protocols, and telnet policy helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "BackpressurePolicy",
    "ByteDirection",
    "ClientFilter",
    "ClientMetadata",
    "DefaultTelnetPolicy",
    "InterceptAction",
    "InterceptResult",
    "SessionLifecycle",
    "TelnetPolicy",
    "UpstreamPipe",
    "WireEventKind",
]


class SessionLifecycle(Enum):
    CREATED = auto()
    CONNECTING = auto()
    NEGOTIATED = auto()
    CONNECTED = auto()
    UPSTREAM_LOST = auto()
    RECONNECTING = auto()
    CLIENT_ATTACHED = auto()
    SHUTDOWN = auto()


class InterceptAction(Enum):
    PASS = auto()
    REPLACE = auto()
    CONSUME = auto()
    DEFER = auto()
    INJECT = auto()


class BackpressurePolicy(Enum):
    DROP_OLDEST = auto()
    DROP_NEWEST = auto()
    DISCONNECT = auto()


class ByteDirection(Enum):
    UPSTREAM_TO_APP = auto()
    CLIENT_TO_UPSTREAM = auto()


class WireEventKind(Enum):
    IAC = auto()
    NEGOTIATION = auto()
    DIAGNOSTIC = auto()


@dataclass(slots=True)
class InterceptResult:
    action: InterceptAction = InterceptAction.PASS
    payload: bytes | None = None

    @staticmethod
    def pass_() -> InterceptResult:
        return InterceptResult(InterceptAction.PASS)

    @staticmethod
    def replace(payload: bytes) -> InterceptResult:
        return InterceptResult(InterceptAction.REPLACE, payload)

    @staticmethod
    def consume() -> InterceptResult:
        return InterceptResult(InterceptAction.CONSUME)

    @staticmethod
    def defer() -> InterceptResult:
        return InterceptResult(InterceptAction.DEFER)

    @staticmethod
    def inject(payload: bytes) -> InterceptResult:
        return InterceptResult(InterceptAction.INJECT, payload)


@dataclass
class ClientMetadata:
    client_id: str
    tags: set[str] = field(default_factory=set)
    attributes: dict[str, str] = field(default_factory=dict)
    backpressure: BackpressurePolicy = BackpressurePolicy.DROP_OLDEST
    queue_capacity: int = 64


@dataclass
class ClientFilter:
    require_any_tag: list[str] | None = None
    exclude_tags: list[str] | None = None
    predicate: Callable[[ClientMetadata], bool] | None = None

    def matches(self, meta: ClientMetadata) -> bool:
        if self.exclude_tags and any(t in meta.tags for t in self.exclude_tags):
            return False
        if self.require_any_tag and not any(t in meta.tags for t in self.require_any_tag):
            return False
        return not (self.predicate is not None and not self.predicate(meta))


class UpstreamPipe(Protocol):
    @property
    def is_connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def send(self, data: bytes) -> None: ...

    async def receive(self) -> bytes: ...


class TelnetPolicy(Protocol):
    @property
    def terminal_type(self) -> str: ...

    @property
    def window_size(self) -> tuple[int, int]: ...

    def on_option(self, command: int, option: int) -> bytes: ...

    def on_subnegotiation(self, option: int, body: bytes) -> bytes: ...


@dataclass
class DefaultTelnetPolicy:
    terminal_type: str = "ANSI"
    window_size: tuple[int, int] = (80, 25)

    def on_option(self, command: int, option: int) -> bytes:
        iac, will, wont, do_cmd, dont = 255, 251, 252, 253, 254
        if command == do_cmd:
            return bytes((iac, will, option))
        if command == will:
            return bytes((iac, do_cmd, option))
        if command == wont:
            return bytes((iac, dont, option))
        if command == dont:
            return bytes((iac, wont, option))
        return b""

    def on_subnegotiation(self, option: int, body: bytes) -> bytes:
        iac, sb, se = 255, 250, 240
        if option == 24 and body and body[0] == 1:
            term = self.terminal_type.encode("ascii", errors="replace")
            return bytes((iac, sb, 24, 0)) + term + bytes((iac, se))
        if option == 31:
            cols, rows = self.window_size
            return bytes(
                (
                    iac,
                    sb,
                    31,
                    (cols >> 8) & 0xFF,
                    cols & 0xFF,
                    (rows >> 8) & 0xFF,
                    rows & 0xFF,
                    iac,
                    se,
                )
            )
        return b""
