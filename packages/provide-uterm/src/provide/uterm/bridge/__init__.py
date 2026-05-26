#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Namespace package for ``provide.uterm.bridge``.

Both ``provide-uterm`` (this distribution; ``contracts`` and the hijack
``coordinator``) and ``provide-uterm-server`` (the runtime implementation)
contribute modules to this dotted name. We keep an explicit ``__init__.py``
for backward compatibility with tools that don't follow PEP 420 namespace
packages, and use ``pkgutil.extend_path`` so the merged set of submodules
is visible at import time.

The single-session hijack coordinator (pure stdlib, ~120 LOC) is re-exported
here as a convenience so callers don't need to import the dotted submodule
path directly.
"""

import pkgutil

__path__ = pkgutil.extend_path(__path__, __name__)

from provide.uterm.bridge.coordinator import (
    AcquireResult,
    HijackCoordinator,
    HijackSession,
)

__all__ = ["AcquireResult", "HijackCoordinator", "HijackSession"]
