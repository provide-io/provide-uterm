#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""In-process multi-client proxy session API.

Hosts (protocol-aware proxies) attach interceptors and clients without CLI/HTTP.
Parity target with Go ``embed`` and C# ``Provide.Uterm.Embed``.
"""

from __future__ import annotations

from provide.uterm.embed.hub import EmbedHub
from provide.uterm.embed.session import (
    ByteInterceptor,
    ClientHandle,
    EmbedSession,
    InterceptContext,
    MemoryUpstream,
    PassThroughInterceptor,
)
from provide.uterm.embed.types import (
    BackpressurePolicy,
    ByteDirection,
    ClientFilter,
    ClientMetadata,
    DefaultTelnetPolicy,
    InterceptAction,
    InterceptResult,
    SessionLifecycle,
    TelnetPolicy,
    UpstreamPipe,
    WireEventKind,
)

__all__ = [
    "BackpressurePolicy",
    "ByteDirection",
    "ByteInterceptor",
    "ClientFilter",
    "ClientHandle",
    "ClientMetadata",
    "DefaultTelnetPolicy",
    "EmbedHub",
    "EmbedSession",
    "InterceptAction",
    "InterceptContext",
    "InterceptResult",
    "MemoryUpstream",
    "PassThroughInterceptor",
    "SessionLifecycle",
    "TelnetPolicy",
    "UpstreamPipe",
    "WireEventKind",
]
