#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Public exports for the split TermHub implementation."""

from __future__ import annotations

# Helpers / type aliases / the resolver-error live in ``core_helpers``; only
# ``TermHub`` itself comes from ``core_impl``. The public ``hub.core`` namespace
# (and everything importing from it) is unchanged — same names, same objects.
from provide.uterm.server.bridge.hub.core_helpers import (
    BrowserRoleResolutionError,
    BrowserRoleResolver,
    HijackStateCallback,
    MetricCallback,
    ResumeCallback,
    WorkerEmptyCallback,
    _encode_browser_frame,
    _encode_worker_frame,
    _mono_to_wall,
)
from provide.uterm.server.bridge.hub.core_impl import TermHub

__all__ = [
    "BrowserRoleResolutionError",
    "BrowserRoleResolver",
    "HijackStateCallback",
    "MetricCallback",
    "ResumeCallback",
    "TermHub",
    "WorkerEmptyCallback",
    "_encode_browser_frame",
    "_encode_worker_frame",
    "_mono_to_wall",
]
