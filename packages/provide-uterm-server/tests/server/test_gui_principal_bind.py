#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""M1: GUI inject is principal-bound to hijack lease acquired_by."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request

from provide.uterm.server.bridge.routes.rest_gui import _require_graphical_session


def _request(subject: str | None) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [],
        "query_string": b"",
    }
    req = Request(scope)
    if subject is not None:
        req.state.uterm_principal = MagicMock(subject_id=subject)
    else:
        req.state.uterm_principal = None
    return req


@pytest.mark.asyncio
async def test_gui_inject_forbidden_for_non_owner() -> None:
    hub = MagicMock()
    hs = MagicMock()
    hs.acquired_by = "alice"
    hub.get_rest_session = AsyncMock(return_value=hs)
    st = MagicMock()
    st.graphical_session = object()
    hub.registry.get.return_value = st

    resp = await _require_graphical_session(hub, _request("bob"), "w1", "h1")
    assert getattr(resp, "status_code", None) == 403


@pytest.mark.asyncio
async def test_gui_inject_allowed_for_owner() -> None:
    hub = MagicMock()
    hs = MagicMock()
    hs.acquired_by = "alice"
    hub.get_rest_session = AsyncMock(return_value=hs)
    gui = object()
    st = MagicMock()
    st.graphical_session = gui
    hub.registry.get.return_value = st

    got = await _require_graphical_session(hub, _request("alice"), "w1", "h1")
    assert got is gui
