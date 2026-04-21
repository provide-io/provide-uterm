#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from provide.terminal.bridge.hub import TermHub
from provide.terminal.bridge.hub.ext import (
    EVENT_HIJACK_ACQUIRED,
    EVENT_HIJACK_RELEASED,
    EVENT_SESSION_REGISTERED,
)


@pytest.mark.asyncio
async def test_session_registration_telemetry() -> None:
    hub = TermHub()
    ws = AsyncMock()

    with patch("provide.terminal.bridge.hub.connections.logger") as mock_logger:
        await hub.register_browser("w1", ws, "operator")

        # Verify logger.info was called with DAS event
        mock_logger.info.assert_any_call(
            EVENT_SESSION_REGISTERED, worker_id="w1", session_type="browser", role="operator"
        )


@pytest.mark.asyncio
async def test_hijack_lifecycle_telemetry() -> None:
    hub = TermHub()
    ws = AsyncMock()
    worker_ws = AsyncMock()
    worker_id = "w1"

    await hub.register_worker(worker_id, worker_ws)
    await hub.register_browser(worker_id, ws, "admin")

    with patch("provide.terminal.bridge.hub.ownership.logger") as mock_logger:
        # Acquire hijack
        from unittest.mock import ANY

        await hub.try_acquire_ws_hijack(worker_id, ws)
        mock_logger.info.assert_any_call(
            EVENT_HIJACK_ACQUIRED, worker_id=worker_id, hijack_type="dashboard", lease_s=ANY
        )

        # Release hijack
        await hub.try_release_ws_hijack(worker_id, ws)
        mock_logger.info.assert_any_call(EVENT_HIJACK_RELEASED, worker_id=worker_id, hijack_type="dashboard")
