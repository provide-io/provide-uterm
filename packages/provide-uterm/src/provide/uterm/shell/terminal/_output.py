#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""provide-uterm wire-protocol frame builders for provide.uterm.shell.terminal."""

from __future__ import annotations

import time
from typing import Any


def term(data: str, ts: float | None = None) -> dict[str, Any]:
    """Build a ``term`` worker-protocol frame."""
    return {"type": "term", "data": data, "ts": ts or time.time()}


def worker_hello(input_mode: str = "open") -> dict[str, Any]:
    """Build a ``worker_hello`` frame declaring the session input mode.

    Advertises the protocol-version range so the server can negotiate
    against its own. Older workers that omitted ``protocol`` default to
    ``{"min": 1, "max": 1}`` on the server side, so this is backward-
    compatible.
    """
    from provide.uterm.bridge.contracts import (
        MAX_PROTOCOL_VERSION,
        MIN_PROTOCOL_VERSION,
        PREFERRED_PROTOCOL_VERSION,
    )

    return {
        "type": "worker_hello",
        "input_mode": input_mode,
        "ts": time.time(),
        "protocol": {
            "min": MIN_PROTOCOL_VERSION,
            "max": MAX_PROTOCOL_VERSION,
            "preferred": PREFERRED_PROTOCOL_VERSION,
        },
    }
