#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Module-level helpers and type aliases for :mod:`...hub.core_impl`.

These free functions, callback typedefs and the
:class:`BrowserRoleResolutionError` exception were split out of
``core_impl.py`` to keep that module under 500 LOC. ``core_impl`` (and
therefore the public ``hub.core`` facade) re-exports every name defined
here, so the import surface is unchanged: ``from
provide.uterm.server.bridge.hub.core import _encode_browser_frame`` etc.
continues to resolve.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from fastapi import WebSocket

from provide.uterm.control_channel import encode_control_frame, encode_terminal_data
from provide.uterm.server.bridge.hub.resume import ResumeSession

HijackStateCallback = Callable[[str, bool, str | None], Awaitable[None] | None]
BrowserRoleResolver = Callable[[WebSocket, str], str | Awaitable[str | None] | None]
MetricCallback = Callable[[str, int], None]
WorkerEmptyCallback = Callable[[str], Coroutine[Any, Any, None]]
ResumeCallback = Callable[[str, ResumeSession], Awaitable[bool]]


def _encode_browser_frame(msg: dict[str, Any]) -> str:
    if str(msg.get("type") or "") == "term":
        return encode_terminal_data(str(msg.get("data") or ""))
    return encode_control_frame(msg)


def _encode_worker_frame(msg: dict[str, Any]) -> str:
    if str(msg.get("type") or "") == "input":
        return encode_terminal_data(str(msg.get("data") or ""))
    return encode_control_frame(msg)


def _mono_to_wall(mono_ts: float | None) -> float | None:
    """Convert a monotonic timestamp to wall-clock for external consumers."""
    if mono_ts is None:
        return None
    return time.time() + (mono_ts - time.monotonic())


class BrowserRoleResolutionError(RuntimeError):
    """Raised when a browser-role resolver fails and the WS should be rejected."""


__all__ = [
    "BrowserRoleResolutionError",
    "BrowserRoleResolver",
    "HijackStateCallback",
    "MetricCallback",
    "ResumeCallback",
    "WorkerEmptyCallback",
    "_encode_browser_frame",
    "_encode_worker_frame",
    "_mono_to_wall",
]
