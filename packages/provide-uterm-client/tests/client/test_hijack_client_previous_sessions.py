#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""HijackClient reads the capture sessions a server kept after they ended."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from httpx2 import ASGITransport

from provide.uterm.client.hijack import HijackClient

SID = "pam-alice-capture-1-10"


def _app(tmp_path: Path, keep_days: int = 7) -> Any:
    from provide.uterm.server.app import create_server_app
    from provide.uterm.server.config import config_from_mapping

    cfg = config_from_mapping(
        {
            "server": {"host": "127.0.0.1", "port": 0},
            "auth": {
                "mode": "header",
                "header_mode_acknowledged": True,
                "worker_bearer_token": "test-bearer-token-32-chars-long-x",
            },
            "sessions": [],
            "recording": {"directory": str(tmp_path / "recordings")},
            "session_persistence": {"previous_keep_days": keep_days, "state_dir": str(tmp_path / "state")},
        }
    )
    return create_server_app(cfg, api_only=True)


async def _end_one(app: Any) -> None:
    from provide.uterm.server.config_schema import SessionDefinition

    store = app.state.uterm_capture_state
    definition = SessionDefinition(
        session_id=SID,
        connector_type="pty_capture",
        connector_config={"socket_path": "/run/c/s.sock"},
        owner="alice",
        visibility="private",
    )
    store.track(definition, "i")
    await store.save_screen(SID, {"screen": "last screen", "cols": 80, "rows": 24}, ["above"])
    await store.end(SID)


def _client(app: Any, principal: str) -> HijackClient:
    return HijackClient(
        "http://test",
        transport=ASGITransport(app=app),
        headers={"X-Uterm-Principal": principal, "X-Uterm-Role": "viewer"},
    )


async def test_the_owner_lists_and_reads_a_previous_session(tmp_path: Path) -> None:
    app = _app(tmp_path)
    await _end_one(app)
    async with _client(app, "alice") as c:
        ok, listed = await c.list_previous_sessions()
        assert ok
        assert [entry["id"] for entry in listed["sessions"]] == [SID]
        ok, read = await c.get_previous_session(SID)
    assert ok
    assert (read["screen"], read["scrollback"]) == ("last screen", ["above"])


async def test_another_caller_is_refused(tmp_path: Path) -> None:
    app = _app(tmp_path)
    await _end_one(app)
    async with _client(app, "mallory") as c:
        ok, listed = await c.list_previous_sessions()
        assert ok
        assert listed == {"sessions": []}
        ok, read = await c.get_previous_session(SID)
    assert not ok
    assert read["detail"] == "insufficient privileges"


async def test_an_unsafe_id_is_refused_before_a_request(tmp_path: Path) -> None:
    async with _client(_app(tmp_path), "alice") as c:
        with pytest.raises(ValueError, match="session_id"):
            await c.get_previous_session("../health")
