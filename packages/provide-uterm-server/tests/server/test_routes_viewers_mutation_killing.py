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
from unittest.mock import ANY, AsyncMock, MagicMock, call

import pytest
from fastapi import APIRouter, HTTPException

from provide.uterm.server.routes import viewers

_SID = "s-1"


def _request(
    *, can_mutate: bool = True, definition: Any = "present", admin: bool = False, body: bytes | list[bytes] = b""
) -> SimpleNamespace:
    az = MagicMock(name="authz")
    az.can_mutate_session = AsyncMock(return_value=can_mutate)
    az.is_admin = AsyncMock(return_value=admin)
    az.revoke_share_viewers = MagicMock(return_value=123.5)
    az.revoke_share_viewer_subject = MagicMock(return_value=124.5)
    az.share_viewer_revoked_sessions = MagicMock(return_value=[])
    registry = MagicMock(name="registry")
    resolved = SimpleNamespace(session_id=_SID) if definition == "present" else definition
    registry.get_definition = AsyncMock(return_value=resolved)
    registry.unknown_sessions = AsyncMock(return_value=[])
    hub = object()
    state = SimpleNamespace(uterm_authz=az, uterm_registry=registry, uterm_hub=hub)
    return SimpleNamespace(
        app=SimpleNamespace(state=state),
        state=SimpleNamespace(uterm_principal=SimpleNamespace(subject_id="owner-1", roles=set())),
        client=SimpleNamespace(host="10.0.0.9"),
        stream=_Stream([body] if isinstance(body, bytes) else body),
    )


class _Stream:
    """``request.stream`` over fixed chunks, recording how many were read."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.read = 0

    async def _iterate(self) -> Any:
        for chunk in self.chunks:
            self.read += 1
            yield chunk

    def __call__(self) -> Any:
        return self._iterate()


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
    az.revoke_share_viewers.assert_called_once_with(_SID, revoker="owner-1", on_evict=ANY)
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


async def test_a_global_admins_revoke_is_not_counted_against_a_principal(closer: AsyncMock, audit: MagicMock) -> None:
    request = _request(admin=True)
    await viewers.revoke_viewers(request, _SID)  # type: ignore[arg-type]
    az = request.app.state.uterm_authz
    az.is_admin.assert_awaited_once_with(request.state.uterm_principal)
    az.revoke_share_viewers.assert_called_once_with(_SID, revoker=None, on_evict=ANY)


async def test_revoke_leaves_an_audit_record(closer: AsyncMock, audit: MagicMock) -> None:
    await viewers.revoke_viewers(_request(), _SID)  # type: ignore[arg-type]
    audit.assert_called_once_with(
        "session.viewers.revoke",
        principal="owner-1",
        session_id=_SID,
        detail={"closed": 5, "revoked_at": 123.5},
        source_ip="10.0.0.9",
    )


async def test_a_cutoff_the_cap_evicts_is_audited_by_session_and_principal(closer: AsyncMock, audit: MagicMock) -> None:
    request = _request()

    def revoke(session_id: str, *, revoker: str | None, on_evict: Any) -> float:
        on_evict("s-old")
        return 123.5

    request.app.state.uterm_authz.revoke_share_viewers = MagicMock(side_effect=revoke)
    await viewers.revoke_viewers(request, _SID)  # type: ignore[arg-type]
    assert audit.mock_calls == [
        call(
            "session.viewers.cutoff_evicted",
            principal="owner-1",
            session_id="s-old",
            detail={"revoked_session_id": _SID},
            source_ip="10.0.0.9",
        ),
        call(
            "session.viewers.revoke",
            principal="owner-1",
            session_id=_SID,
            detail={"closed": 5, "revoked_at": 123.5},
            source_ip="10.0.0.9",
        ),
    ]


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


async def test_cutoffs_of_deleted_sessions_are_forgotten_before_the_revoke(closer: AsyncMock, audit: MagicMock) -> None:
    request = _request()
    az = request.app.state.uterm_authz
    order = MagicMock()
    az.share_viewer_revoked_sessions = MagicMock(return_value=["gone", "alive"])
    az.forget_share_viewers = order.forget
    az.revoke_share_viewers = order.revoke
    order.revoke.return_value = 123.5
    registry = request.app.state.uterm_registry
    registry.unknown_sessions = AsyncMock(return_value=["gone"])
    await viewers.revoke_viewers(request, _SID)  # type: ignore[arg-type]
    # One pass over the held cutoffs, not a registry lookup per cutoff.
    registry.unknown_sessions.assert_awaited_once_with(["gone", "alive"])
    registry.get_definition.assert_awaited_once_with(_SID)
    assert order.mock_calls[0] == call.forget("gone")
    assert order.mock_calls[1] == call.revoke(_SID, revoker="owner-1", on_evict=ANY)
    assert len(order.mock_calls) == 2


async def test_a_full_cutoff_table_is_a_429_that_closes_nothing(closer: AsyncMock, audit: MagicMock) -> None:
    from provide.uterm.server.authorization import ShareViewerRevocationsFullError

    request = _request()
    request.app.state.uterm_authz.revoke_share_viewers = MagicMock(side_effect=ShareViewerRevocationsFullError())
    with pytest.raises(HTTPException) as caught:
        await viewers.revoke_viewers(request, _SID)  # type: ignore[arg-type]
    assert caught.value.status_code == 429
    assert caught.value.detail == "too many sessions with revoked viewers; try again later"
    closer.assert_not_awaited()
    audit.assert_not_called()


_SUBJECT_BODY = b'{"subject": "share:s-1:blackbetty:7"}'


async def test_a_subject_revoke_records_only_that_subjects_cutoff(
    closer: AsyncMock, audit: MagicMock, streams: MagicMock
) -> None:
    request = _request(body=_SUBJECT_BODY)
    result = await viewers.revoke_viewers(request, _SID)  # type: ignore[arg-type]
    az = request.app.state.uterm_authz
    az.revoke_share_viewer_subject.assert_called_once_with(_SID, "share:s-1:blackbetty:7")
    az.revoke_share_viewers.assert_not_called()
    az.is_admin.assert_not_awaited()
    closer.assert_awaited_once_with(request.app.state.uterm_hub, az, _SID)
    streams.revoke.assert_called_once_with(az, _SID)
    assert result == {
        "session_id": _SID,
        "revoked_at": 124.5,
        "closed": 5,
        "close_code": 4410,
        "close_reason": "viewer access revoked",
        "subject": "share:s-1:blackbetty:7",
    }
    audit.assert_called_once_with(
        "session.viewers.revoke",
        principal="owner-1",
        session_id=_SID,
        detail={"closed": 5, "revoked_at": 124.5, "subject": "share:s-1:blackbetty:7"},
        source_ip="10.0.0.9",
    )


async def test_a_subject_revoke_also_forgets_deleted_sessions_first(closer: AsyncMock, audit: MagicMock) -> None:
    request = _request(body=_SUBJECT_BODY)
    az = request.app.state.uterm_authz
    order = MagicMock()
    az.share_viewer_revoked_sessions = MagicMock(return_value=["gone"])
    az.forget_share_viewers = order.forget
    az.revoke_share_viewer_subject = order.revoke
    order.revoke.return_value = 124.5
    request.app.state.uterm_registry.unknown_sessions = AsyncMock(return_value=["gone"])
    await viewers.revoke_viewers(request, _SID)  # type: ignore[arg-type]
    assert order.mock_calls == [call.forget("gone"), call.revoke(_SID, "share:s-1:blackbetty:7")]


async def test_a_session_full_of_subject_cutoffs_is_a_429_that_closes_nothing(
    closer: AsyncMock, audit: MagicMock
) -> None:
    from provide.uterm.server.authorization import ShareViewerSubjectRevocationsFullError

    request = _request(body=_SUBJECT_BODY)
    request.app.state.uterm_authz.revoke_share_viewer_subject = MagicMock(
        side_effect=ShareViewerSubjectRevocationsFullError()
    )
    with pytest.raises(HTTPException) as caught:
        await viewers.revoke_viewers(request, _SID)  # type: ignore[arg-type]
    assert caught.value.status_code == 429
    assert caught.value.detail == "too many viewers of this session revoked one by one; revoke them all instead"
    closer.assert_not_awaited()
    audit.assert_not_called()


async def test_a_refused_body_prunes_and_records_nothing(closer: AsyncMock, audit: MagicMock) -> None:
    request = _request(body=b'{"subject": 7}')
    with pytest.raises(HTTPException) as caught:
        await viewers.revoke_viewers(request, _SID)  # type: ignore[arg-type]
    assert caught.value.status_code == 422
    assert caught.value.detail == "invalid request body"
    az = request.app.state.uterm_authz
    request.app.state.uterm_registry.unknown_sessions.assert_not_awaited()
    az.revoke_share_viewers.assert_not_called()
    az.revoke_share_viewer_subject.assert_not_called()
    closer.assert_not_awaited()


async def test_a_refused_caller_never_has_its_body_read(closer: AsyncMock, audit: MagicMock) -> None:
    request = _request(can_mutate=False, body=_SUBJECT_BODY)
    with pytest.raises(HTTPException):
        await viewers.revoke_viewers(request, _SID)  # type: ignore[arg-type]
    assert request.stream.read == 0


async def test_a_body_in_several_chunks_is_read_whole(closer: AsyncMock, audit: MagicMock) -> None:
    request = _request(body=[b'{"subject": ', b'"share:s-1:', b'blackbetty:7"}'])
    await viewers.revoke_viewers(request, _SID)  # type: ignore[arg-type]
    request.app.state.uterm_authz.revoke_share_viewer_subject.assert_called_once_with(_SID, "share:s-1:blackbetty:7")


async def test_a_body_past_the_cap_is_refused_without_reading_on(closer: AsyncMock, audit: MagicMock) -> None:
    from provide.uterm.server.authorization import SHARE_VIEWER_REVOKE_BODY_MAX_BYTES

    request = _request(body=[b" " * SHARE_VIEWER_REVOKE_BODY_MAX_BYTES, b"{", b"}"])
    with pytest.raises(HTTPException) as caught:
        await viewers.revoke_viewers(request, _SID)  # type: ignore[arg-type]
    assert caught.value.status_code == 422
    assert caught.value.detail == "invalid request body"
    # The chunk that crossed the cap was the last one read.
    assert request.stream.read == 2
    request.app.state.uterm_authz.revoke_share_viewers.assert_not_called()


async def test_a_body_exactly_at_the_cap_is_read(closer: AsyncMock, audit: MagicMock) -> None:
    from provide.uterm.server.authorization import SHARE_VIEWER_REVOKE_BODY_MAX_BYTES

    request = _request(body=[b" " * (SHARE_VIEWER_REVOKE_BODY_MAX_BYTES - 2), b"{}"])
    await viewers.revoke_viewers(request, _SID)  # type: ignore[arg-type]
    request.app.state.uterm_authz.revoke_share_viewers.assert_called_once()


async def test_a_full_subject_table_is_a_429_that_closes_nothing(closer: AsyncMock, audit: MagicMock) -> None:
    from provide.uterm.server.authorization import ShareViewerRevocationsFullError

    request = _request(body=_SUBJECT_BODY)
    request.app.state.uterm_authz.revoke_share_viewer_subject = MagicMock(side_effect=ShareViewerRevocationsFullError())
    with pytest.raises(HTTPException) as caught:
        await viewers.revoke_viewers(request, _SID)  # type: ignore[arg-type]
    assert caught.value.status_code == 429
    assert caught.value.detail == "too many viewers revoked one by one; try again later"
    closer.assert_not_awaited()
    audit.assert_not_called()
