#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Shared pytest fixtures for provide-terminal tests."""

from __future__ import annotations

import socket

import pytest


@pytest.fixture()
def free_port() -> int:
    """Return a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# event_loop fixture intentionally omitted: pytest-asyncio >= 0.21 with
# asyncio_mode="auto" manages per-test loops automatically.  A custom
# event_loop fixture at function scope conflicts with the auto-mode
# machinery and can cause asyncio.Lock cross-loop corruption.
