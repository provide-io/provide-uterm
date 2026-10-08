#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Applying a connector_config change to a running session without a restart.

A PATCH used to change only the stored definition, so a caller had to restart
the session for the running connector to see it -- and a restart closes a
capture socket under the program writing to it. A connector that can take the
change in place now does, and the status says whether a restart is still owed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.server.models import RecordingConfig, SessionDefinition
from provide.uterm.server.runtime import HostedSessionRuntime


def _runtime() -> HostedSessionRuntime:
    return HostedSessionRuntime(
        SessionDefinition(
            session_id="cap-1",
            display_name="Capture",
            connector_type="shell",
            auto_start=False,
            input_mode="hijack",
        ),
        public_base_url="http://localhost:9999",
        recording=RecordingConfig(),
    )


async def test_a_connector_that_reconfigures_live_owes_no_restart() -> None:
    runtime = _runtime()
    connector = MagicMock()
    connector.reconfigure = MagicMock(return_value=True)
    runtime._connector = connector

    await runtime.reconfigure({"socket_path": "/run/cap.sock", "stdin_socket_path": "/run/in.sock"})

    # It sees the config the connector would have been built with, mode included.
    connector.reconfigure.assert_called_once_with(
        {"socket_path": "/run/cap.sock", "stdin_socket_path": "/run/in.sock", "input_mode": "hijack"}
    )
    assert runtime.status().config_pending_restart is False


@pytest.mark.parametrize("live", [False, None])
async def test_a_change_the_connector_cannot_take_live_is_owed_a_restart(live: bool | None) -> None:
    runtime = _runtime()
    connector = MagicMock(spec=["start", "stop", "is_connected"] + (["reconfigure"] if live is not None else []))
    if live is not None:
        connector.reconfigure = MagicMock(return_value=live)
    runtime._connector = connector

    await runtime.reconfigure({"socket_path": "/run/other.sock"})

    assert runtime.status().config_pending_restart is True


async def test_a_stopped_session_owes_nothing() -> None:
    # The next start builds the connector from the definition anyway.
    runtime = _runtime()
    await runtime.reconfigure({"socket_path": "/run/cap.sock"})
    assert runtime.status().config_pending_restart is False


async def test_starting_a_connector_settles_what_was_owed(monkeypatch) -> None:
    runtime = _runtime()
    runtime._config_pending_restart = True
    built = AsyncMock()
    built.is_connected = MagicMock(return_value=True)
    monkeypatch.setattr("provide.uterm.server.runtime.build_connector", lambda *_: built)

    await runtime._start_connector()

    assert runtime.status().config_pending_restart is False
