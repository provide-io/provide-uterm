#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""provide-terminal wire-protocol frame builders for provide.terminal.shell.terminal."""

from __future__ import annotations

import time
from typing import Any


def term(data: str, ts: float | None = None) -> dict[str, Any]:
    """Build a ``term`` worker-protocol frame."""
    return {"type": "term", "data": data, "ts": ts or time.time()}


def worker_hello(input_mode: str = "open") -> dict[str, Any]:
    """Build a ``worker_hello`` frame declaring the session input mode."""
    return {"type": "worker_hello", "input_mode": input_mode, "ts": time.time()}
