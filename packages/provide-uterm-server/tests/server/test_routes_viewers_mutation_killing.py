#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing suite for ``routes/viewers.py``.

The end-to-end suite (``test_viewer_revocation.py``) proves the behaviour; this
one pins what an end-to-end run cannot see: the exact capability the handler
asks for, the order it does things in (authorize before revoking -- a refused
caller must not move the cutoff), the arguments it forwards, and the audit
record it leaves.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import APIRouter, HTTPException

from provide.uterm.server.routes import viewers

_SID = "s-1"


def _request(*, can_mutate: bool = True, definition: Any = "present") -> SimpleNamespace:
    az = MagicMock(name="authz")
    az.can_mutate_session = AsyncMock(return_value=can_mutate)
    az.revoke_share_viewers = MagicMock(return_value=123.5)
    registry = MagicMock(name="registry")
    resolved = SimpleNamespace(session_id=_SID) if definition == "present" else definition
    registry.get_definition = AsyncMock(return_value=resolved)
    hub = object()
    state = SimpleNamespace(uterm_authz=az, uterm_registry=registry, uterm_hub=hub)
    return SimpleNamespace(
        app=SimpleNamespace(state=state),
        state=SimpleNamespace(uterm_principal=SimpleNamespace(subject_id="owner-1", roles=set())),
        client=SimpleNamespace(host="10.0.0.9"),
    )


@pytest.fixture
def closer(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock(return_value=2)
    monkeypatch.setattr(viewers, "close_revoked_viewers", mock)
    return mock


@pytest.fixture(autouse=True)
def streams(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    registry = MagicMock(name="streams")
    registry.revoke = MagicMock(return_value=3)
    finder = MagicMock(return_value=registry)
    monkeypatch.setattr(viewers, "share_viewer_streams", finder)
    registry.finder = finder
    return registry


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr(viewers, "audit_event", mock)
    return mock


async def test_revoke_asks_for_the_update_capability_on_this_session(closer: AsyncMock, audit: MagicMock) -> None:
    request = _request()
    await viewers.revoke_viewers(request, _SID)  # type: ignore[arg-type]
    az = request.app.state.uterm_authz
    az.can_mutate_session.assert_awaited_once()
    p, definition, action = az.can_mutate_session.await_args.args
    assert p is request.state.uterm_principal
    assert definition.session_id == _SID
    assert action == "session.control.update"
    request.app.state.uterm_registry.get_definition.assert_awaited_once_with(_SID)


async def test_revoke_records_the_cutoff_then_closes_through_the_hub(
    closer: AsyncMock, audit: MagicMock, streams: MagicMock
) -> None:
    request = _request()
    result = await viewers.revoke_viewers(request, _SID)  # type: ignore[arg-type]
    az = request.app.state.uterm_authz
    az.revoke_share_viewers.assert_called_once_with(_SID)
    closer.assert_awaited_once_with(request.app.state.uterm_hub, az, _SID)
    # Event streams are ended too, and counted with the sockets: 2 + 3.
    streams.finder.assert_called_once_with(request.app)
    streams.revoke.assert_called_once_with(az, _SID)
    assert result == {
        "session_id": _SID,
        "revoked_at": 123.5,
        "closed": 5,
        "close_code": 4410,
        "close_reason": "viewer access revoked",
    }


async def test_revoke_leaves_an_audit_record(closer: AsyncMock, audit: MagicMock) -> None:
    await viewers.revoke_viewers(_request(), _SID)  # type: ignore[arg-type]
    audit.assert_called_once_with(
        "session.viewers.revoke",
        principal="owner-1",
        session_id=_SID,
        detail={"closed": 5, "revoked_at": 123.5},
        source_ip="10.0.0.9",
    )


async def test_a_refused_caller_moves_no_cutoff_and_closes_nothing(closer: AsyncMock, audit: MagicMock) -> None:
    request = _request(can_mutate=False)
    with pytest.raises(HTTPException) as caught:
        await viewers.revoke_viewers(request, _SID)  # type: ignore[arg-type]
    assert caught.value.status_code == 403
    assert caught.value.detail == "insufficient privileges"
    request.app.state.uterm_authz.revoke_share_viewers.assert_not_called()
    closer.assert_not_awaited()
    audit.assert_not_called()


async def test_an_unknown_session_is_a_404_before_any_authorization(closer: AsyncMock, audit: MagicMock) -> None:
    request = _request(definition=None)
    with pytest.raises(HTTPException) as caught:
        await viewers.revoke_viewers(request, _SID)  # type: ignore[arg-type]
    assert caught.value.status_code == 404
    request.app.state.uterm_authz.can_mutate_session.assert_not_awaited()
    closer.assert_not_awaited()


def test_the_route_is_a_post_on_the_session_viewers_path() -> None:
    router = APIRouter()
    viewers.register_viewer_routes(router)
    (route,) = router.routes
    assert route.path == "/sessions/{session_id}/viewers/revoke"  # type: ignore[attr-defined]
    assert route.methods == {"POST"}  # type: ignore[attr-defined]
    assert route.name == "sessions.viewers.revoke"  # type: ignore[attr-defined]
    assert route.endpoint is viewers.revoke_viewers  # type: ignore[attr-defined]
