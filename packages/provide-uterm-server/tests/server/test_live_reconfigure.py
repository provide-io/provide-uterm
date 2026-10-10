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


async def test_a_later_live_change_settles_an_owed_restart() -> None:
    # The connector answers for the WHOLE stored config, so True after an
    # earlier False means the running connector matches it again.
    runtime = _runtime()
    connector = MagicMock()
    connector.reconfigure = MagicMock(side_effect=[False, True])
    runtime._connector = connector

    await runtime.reconfigure({"socket_path": "/run/other.sock"})
    assert runtime.status().config_pending_restart is True
    await runtime.reconfigure({"socket_path": "/run/cap.sock", "cols": 132})
    assert runtime.status().config_pending_restart is False


async def test_a_reverted_capture_socket_settles_the_owed_restart(tmp_path) -> None:
    # The real connector: a socket change is owed, reverting it is not.
    from provide.uterm.pty.capture_connector import CaptureConnector

    runtime = _runtime()
    runtime._connector = CaptureConnector("cap-1", "Capture", {"socket_path": str(tmp_path / "cap.sock")})

    await runtime.reconfigure({"socket_path": str(tmp_path / "other.sock")})
    assert runtime.status().config_pending_restart is True
    await runtime.reconfigure({"socket_path": str(tmp_path / "cap.sock"), "cols": 132})
    assert runtime.status().config_pending_restart is False


@pytest.mark.parametrize(
    "bad",
    [
        {"cols": "wide"},  # int("wide") -> ValueError
        {"rows": None},  # int(None) -> TypeError
        {"bogus": 1},  # unknown key
    ],
)
def test_a_bad_patch_is_a_422_and_leaves_the_definition_alone(tmp_path, bad: dict[str, object]) -> None:
    from fastapi.testclient import TestClient

    from provide.uterm.pty.capture_connector import CaptureConnector
    from provide.uterm.server import create_server_app, default_server_config

    config = default_server_config()
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    config.recording.directory = tmp_path
    socket_path = str(tmp_path / "cap.sock")
    with TestClient(create_server_app(config)) as client:
        assert client.post("/api/sessions", json={"session_id": "cap-1", "connector_type": "shell"}).status_code < 300
        registry = client.app.state.uterm_registry  # type: ignore[attr-defined]
        runtime = registry._runtimes["cap-1"]
        runtime._connector = CaptureConnector("cap-1", "Capture", {"socket_path": socket_path})
        before = client.get("/api/sessions/cap-1").json()

        r = client.patch(
            "/api/sessions/cap-1",
            json={"display_name": "Renamed", "connector_config": {"socket_path": socket_path, **bad}},
        )

        assert r.status_code == 422
        assert r.json()["detail"].startswith("connector_config rejected: ")
        assert registry._sessions["cap-1"].connector_config == {}
        assert client.get("/api/sessions/cap-1").json() == before
        runtime._connector = None
