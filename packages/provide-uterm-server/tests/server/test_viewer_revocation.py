#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Revoking a session's share viewers ends live watching and refuses their tokens.

A share viewer is a principal whose subject is ``share:{session_id}:...`` and
whose only role is ``viewer`` -- the token an embedder mints so someone can
watch one session. Until now such a token was judged only when the socket
opened, so turning a session private left everyone already watching in place.
``POST /api/sessions/{id}/viewers/revoke`` records a cutoff for the session,
closes every live share-viewer socket with ``4410 viewer access revoked``, and
refuses any share-viewer token issued at or before the cutoff from then on.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from provide.uterm.client import connect_test_ws
from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.authorization import (
    SHARE_VIEWER_REVOCATION_TTL_S,
    SHARE_VIEWER_REVOCATIONS_MAX,
    AuthorizationService,
    is_share_viewer,
)
from provide.uterm.server.bridge.identity import Principal
from provide.uterm.server.models import AuthConfig
from provide.uterm.server.viewer_revocation import (
    VIEWER_REVOKED_CLOSE_CODE,
    VIEWER_REVOKED_REASON,
    close_revoked_viewers,
)

_KEY = "uterm-test-secret-32-byte-minimum-key"
_SESSION = "provide-shell"


def _viewer(session_id: str = "s1", *, iat: object = 100, roles: frozenset[str] | None = None) -> Principal:
    claims: dict[str, Any] = {} if iat is None else {"iat": iat}
    return Principal(
        subject_id=f"share:{session_id}:blackbetty:7",
        roles=frozenset({"viewer"}) if roles is None else roles,
        claims=claims,
    )


def _definition(session_id: str = "s1", visibility: str = "public") -> Any:
    return SimpleNamespace(session_id=session_id, owner=None, visibility=visibility)


# ---------------------------------------------------------------------------
# Who counts as a share viewer
# ---------------------------------------------------------------------------


def test_a_share_subject_holding_only_viewer_is_a_share_viewer() -> None:
    assert is_share_viewer(_viewer("s1"), "s1") is True


def test_a_share_subject_with_no_roles_is_still_a_share_viewer() -> None:
    assert is_share_viewer(_viewer("s1", roles=frozenset()), "s1") is True


def test_a_share_subject_for_another_session_is_not_this_sessions_viewer() -> None:
    assert is_share_viewer(_viewer("s2"), "s1") is False


def test_the_session_id_must_be_followed_by_its_separator() -> None:
    # "share:s1x:..." must not be mistaken for a share of "s1".
    assert is_share_viewer(_viewer("s1x"), "s1") is False


@pytest.mark.parametrize("role", ["operator", "admin"])
def test_a_share_subject_holding_more_than_viewer_is_not_a_share_viewer(role: str) -> None:
    assert is_share_viewer(_viewer("s1", roles=frozenset({"viewer", role})), "s1") is False


def test_a_session_scoped_admin_is_never_a_share_viewer() -> None:
    principal = _viewer("s1")
    principal.admin_session_scope = "s1"
    assert is_share_viewer(principal, "s1") is False


def test_an_ordinary_viewer_is_not_a_share_viewer() -> None:
    principal = Principal(subject_id="alice", roles=frozenset({"viewer"}), claims={"iat": 1})
    assert is_share_viewer(principal, "s1") is False


# ---------------------------------------------------------------------------
# The revocation cutoff
# ---------------------------------------------------------------------------


def test_nothing_is_revoked_before_a_revocation() -> None:
    assert AuthorizationService().share_viewer_revoked(_viewer("s1", iat=1), "s1") is False


def test_revoke_returns_the_cutoff_it_recorded() -> None:
    service = AuthorizationService()
    assert service.revoke_share_viewers("s1", now=500.25) == 500.25
    assert service.share_viewer_revocation("s1") == 500.25
    assert service.share_viewer_revocation("s2") is None


def test_revoke_defaults_to_the_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from provide.uterm.server import authorization

    monkeypatch.setattr(authorization.time, "time", lambda: 1234.5)
    assert AuthorizationService().revoke_share_viewers("s1") == 1234.5


@pytest.mark.parametrize(("iat", "revoked"), [(499, True), (500, True), (500.5, True), (501, False), (10_000, False)])
def test_tokens_issued_at_or_before_the_cutoff_are_revoked(iat: float, revoked: bool) -> None:
    service = AuthorizationService()
    service.revoke_share_viewers("s1", now=500.5)
    assert service.share_viewer_revoked(_viewer("s1", iat=iat), "s1") is revoked


@pytest.mark.parametrize("iat", [None, "600", True, [600]])
def test_a_token_without_a_numeric_iat_is_revoked(iat: object) -> None:
    service = AuthorizationService()
    service.revoke_share_viewers("s1", now=500.0)
    assert service.share_viewer_revoked(_viewer("s1", iat=iat), "s1") is True


def test_revocation_is_per_session() -> None:
    service = AuthorizationService()
    service.revoke_share_viewers("s1", now=500.0)
    assert service.share_viewer_revoked(_viewer("s2", iat=1), "s2") is False


def test_revocation_never_touches_anyone_but_share_viewers() -> None:
    service = AuthorizationService()
    service.revoke_share_viewers("s1", now=500.0)
    operator = _viewer("s1", iat=1, roles=frozenset({"admin"}))
    operator.admin_session_scope = "s1"
    assert service.share_viewer_revoked(operator, "s1") is False
    assert service.share_viewer_revoked(Principal(subject_id="alice", roles=frozenset({"viewer"})), "s1") is False


def test_a_later_revocation_moves_the_cutoff_forward() -> None:
    service = AuthorizationService()
    service.revoke_share_viewers("s1", now=500.0)
    service.revoke_share_viewers("s1", now=900.0)
    assert service.share_viewer_revoked(_viewer("s1", iat=600), "s1") is True


def test_a_full_cutoff_table_refuses_new_sessions_instead_of_evicting_one() -> None:
    from provide.uterm.server.authorization import ShareViewerRevocationsFullError

    service = AuthorizationService()
    base = 1.0e9
    for index in range(SHARE_VIEWER_REVOCATIONS_MAX):
        service.revoke_share_viewers(f"s{index}", now=base + index)
    # A session that already holds a cutoff can always move it forward.
    assert service.revoke_share_viewers("s0", now=base + 1.0e4) == base + 1.0e4
    # A new one is refused rather than pushing anybody's live cutoff out.
    with pytest.raises(ShareViewerRevocationsFullError):
        service.revoke_share_viewers("overflow", now=base + 2.0e4)
    assert service.share_viewer_revocation("overflow") is None
    assert service.share_viewer_revocation("s1") == base + 1
    # Once the oldest cutoffs have aged out there is room again.
    later = base + 1 + SHARE_VIEWER_REVOCATION_TTL_S + 0.5
    assert service.revoke_share_viewers("overflow", now=later) == later
    assert service.share_viewer_revocation("s0") == base + 1.0e4  # still live, kept
    assert service.share_viewer_revocation("s1") is None


def test_tunnel_share_principals_are_not_share_viewers() -> None:
    tunnel_viewer = Principal(
        subject_id="share:s1:viewer", roles=frozenset({"viewer"}), scopes=frozenset({"session.read"}), tunnel_share=True
    )
    assert is_share_viewer(tunnel_viewer, "s1") is False
    service = AuthorizationService()
    service.revoke_share_viewers("s1", now=500.0)
    assert service.share_viewer_revoked(tunnel_viewer, "s1") is False


async def test_reads_are_refused_to_a_revoked_share_viewer_even_on_a_public_session() -> None:
    service = AuthorizationService()
    viewer = _viewer("s1", iat=100)
    assert await service.can_read_session(viewer, _definition("s1")) is True
    assert await service.can_read_recording(viewer, _definition("s1")) is True

    service.revoke_share_viewers("s1", now=200.0)

    assert await service.can_read_session(viewer, _definition("s1")) is False
    assert await service.can_read_recording(viewer, _definition("s1")) is False
    fresh = _viewer("s1", iat=300)
    assert await service.can_read_session(fresh, _definition("s1", visibility="private")) is True
    assert await service.can_read_recording(fresh, _definition("s1", visibility="private")) is True


async def test_a_custom_provider_cannot_readmit_a_revoked_share_viewer() -> None:
    class _AllowAll:
        async def can_read_session(self, _principal: Principal, _session: Any) -> bool:
            return True

        async def can_read_recording(self, _principal: Principal, _session: Any) -> bool:
            return True

    service = AuthorizationService(_AllowAll())  # type: ignore[arg-type]
    service.revoke_share_viewers("s1", now=200.0)
    assert await service.can_read_session(_viewer("s1", iat=100), _definition("s1")) is False
    assert await service.can_read_recording(_viewer("s1", iat=100), _definition("s1")) is False
    assert await service.can_read_session(_viewer("s1", iat=300), _definition("s1")) is True


# ---------------------------------------------------------------------------
# Closing the live sockets
# ---------------------------------------------------------------------------


class _Socket:
    def __init__(self, principal: Principal | None, *, fail: bool = False) -> None:
        self.state = SimpleNamespace() if principal is None else SimpleNamespace(uterm_principal=principal)
        self.closed: list[tuple[int, str]] = []
        self._fail = fail

    async def close(self, code: int, reason: str) -> None:
        self.closed.append((code, reason))
        if self._fail:
            raise RuntimeError("already gone")


def _hub(browsers: dict[Any, str] | None) -> Any:
    workers = {} if browsers is None else {"s1": SimpleNamespace(browsers=browsers)}
    return SimpleNamespace(_lock=asyncio.Lock(), registry=SimpleNamespace(get=workers.get))


async def test_only_revoked_share_viewer_sockets_are_closed() -> None:
    service = AuthorizationService()
    service.revoke_share_viewers("s1", now=500.0)
    watcher = _Socket(_viewer("s1", iat=100))
    broken = _Socket(_viewer("s1", iat=101), fail=True)
    newcomer = _Socket(_viewer("s1", iat=900))
    # Same token, but the socket holds a non-viewer role (a webhook provider can
    # resolve one): it is closed too, since the principal is what is revoked.
    elevated = _Socket(_viewer("s1", iat=100))
    stranger = _Socket(Principal(subject_id="alice", roles=frozenset({"viewer"})))
    unauthenticated = _Socket(None)
    hub = _hub(
        {
            watcher: "viewer",
            broken: "viewer",
            newcomer: "viewer",
            elevated: "operator",
            stranger: "viewer",
            unauthenticated: "viewer",
        }
    )

    closed = await close_revoked_viewers(hub, service, "s1")

    assert closed == 3
    assert watcher.closed == [(VIEWER_REVOKED_CLOSE_CODE, VIEWER_REVOKED_REASON)]
    assert broken.closed == [(VIEWER_REVOKED_CLOSE_CODE, VIEWER_REVOKED_REASON)]
    assert elevated.closed == [(VIEWER_REVOKED_CLOSE_CODE, VIEWER_REVOKED_REASON)]
    for untouched in (newcomer, stranger, unauthenticated):
        assert untouched.closed == []


async def test_closing_viewers_of_a_session_with_no_hub_state_closes_nothing() -> None:
    service = AuthorizationService()
    service.revoke_share_viewers("s1", now=500.0)
    assert await close_revoked_viewers(_hub(None), service, "s1") == 0


def test_the_close_code_is_an_application_code_with_a_generic_reason() -> None:
    assert VIEWER_REVOKED_CLOSE_CODE == 4410
    assert VIEWER_REVOKED_REASON == "viewer access revoked"


# ---------------------------------------------------------------------------
# End to end: the REST endpoint against live sockets
# ---------------------------------------------------------------------------


def _token(sub: str, roles: list[str], *, iat: int | None = None, exp_in: int = 600) -> str:
    issued = int(time.time()) if iat is None else iat
    return jwt.encode(
        {
            "sub": sub,
            "roles": roles,
            "iss": "provide-uterm",
            "aud": "provide-uterm-server",
            "iat": issued,
            "exp": issued + exp_in,
        },
        key=_KEY,
        algorithm="HS256",
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _app() -> Any:
    config = default_server_config()
    config.auth = AuthConfig(
        mode="jwt",
        jwt_public_key_pem=_KEY,
        jwt_algorithms=["HS256"],
        worker_bearer_token=_token("runtime-worker", ["admin"]),
        clock_skew_seconds=30,
    )
    return create_server_app(config, api_only=True)


def _wait_for(browser: Any, frame_type: str) -> dict[str, Any]:
    while True:
        frame = browser.receive_json()
        if frame.get("type") == frame_type:
            return frame


def _read_until_closed(browser: Any) -> None:
    """Read frames until the socket closes; a pong means it was left open."""
    while True:
        frame = browser.receive_json()
        assert frame.get("type") != "pong", "socket was not closed"


def _close_of(browser: Any) -> WebSocketDisconnect:
    # A ping first, so a socket that was wrongly left open answers it and the
    # test fails on the pong instead of waiting forever for a close.
    browser.send_json({"type": "ping"})
    with pytest.raises(WebSocketDisconnect) as caught:
        _read_until_closed(browser)
    return caught.value


def test_revoking_viewers_closes_share_watchers_and_refuses_their_tokens() -> None:
    app = _app()
    watcher_token = _token(f"share:{_SESSION}:blackbetty:7", ["viewer"])
    admin_token = _token("ops", ["admin"])
    path = f"/api/sessions/{_SESSION}/viewers/revoke"

    with (
        TestClient(app) as client,
        connect_test_ws(
            client,
            f"/ws/worker/{_SESSION}/term",
            headers=_bearer(app.state.uterm_config.auth.worker_bearer_token),
        ) as worker,
    ):
        assert worker.receive_json()["type"] == "snapshot_req"
        assert client.get(f"/api/sessions/{_SESSION}", headers=_bearer(watcher_token)).status_code == 200

        with (
            connect_test_ws(client, f"/ws/browser/{_SESSION}/term", headers=_bearer(watcher_token)) as watcher,
            connect_test_ws(client, f"/ws/browser/{_SESSION}/term", headers=_bearer(admin_token)) as admin,
            connect_test_ws(
                client, f"/ws/browser/{_SESSION}/term", headers=_bearer(_token("alice", ["viewer"]))
            ) as plain_viewer,
        ):
            assert _wait_for(watcher, "hello")["role"] == "viewer"
            assert _wait_for(admin, "hello")["role"] == "admin"
            assert _wait_for(plain_viewer, "hello")["role"] == "viewer"

            # A share viewer cannot revoke its own access, nor can a plain viewer.
            assert client.post(path, headers=_bearer(watcher_token)).status_code == 403
            assert client.post(path, headers=_bearer(_token("alice", ["viewer"]))).status_code == 403

            response = client.post(path, headers=_bearer(admin_token))
            assert response.status_code == 200
            body = response.json()
            assert body["session_id"] == _SESSION
            assert body["closed"] == 1
            assert body["close_code"] == VIEWER_REVOKED_CLOSE_CODE
            assert body["close_reason"] == VIEWER_REVOKED_REASON
            assert isinstance(body["revoked_at"], float)

            closed = _close_of(watcher)
            assert closed.code == VIEWER_REVOKED_CLOSE_CODE
            assert closed.reason == VIEWER_REVOKED_REASON

            # The admin and the ordinary viewer keep their sockets.
            admin.send_json({"type": "ping"})
            assert _wait_for(admin, "pong")["type"] == "pong"
            plain_viewer.send_json({"type": "ping"})
            assert _wait_for(plain_viewer, "pong")["type"] == "pong"

        # The revoked token can neither reconnect nor read.
        with (
            pytest.raises(WebSocketDisconnect),
            connect_test_ws(client, f"/ws/browser/{_SESSION}/term", headers=_bearer(watcher_token)) as again,
        ):
            again.receive_json()
        assert client.get(f"/api/sessions/{_SESSION}", headers=_bearer(watcher_token)).status_code == 403

        # A token issued after the cutoff watches again.
        fresh = _token(f"share:{_SESSION}:blackbetty:7", ["viewer"], iat=int(body["revoked_at"]) + 1)
        with connect_test_ws(client, f"/ws/browser/{_SESSION}/term", headers=_bearer(fresh)) as rewatch:
            assert _wait_for(rewatch, "hello")["role"] == "viewer"


def test_an_expired_share_viewer_token_cannot_reconnect() -> None:
    app = _app()
    expired = _token(f"share:{_SESSION}:blackbetty:7", ["viewer"], iat=int(time.time()) - 600, exp_in=60)
    with TestClient(app) as client:
        assert client.get(f"/api/sessions/{_SESSION}", headers=_bearer(expired)).status_code == 401
        with (
            pytest.raises(WebSocketDisconnect),
            connect_test_ws(client, f"/ws/browser/{_SESSION}/term", headers=_bearer(expired)) as browser,
        ):
            browser.receive_json()


def test_revoking_viewers_of_an_unknown_session_is_a_404() -> None:
    app = _app()
    with TestClient(app) as client:
        response = client.post(
            "/api/sessions/no-such-session/viewers/revoke", headers=_bearer(_token("ops", ["admin"]))
        )
        assert response.status_code == 404


def test_revoking_viewers_requires_authentication() -> None:
    app = _app()
    with TestClient(app) as client:
        assert client.post(f"/api/sessions/{_SESSION}/viewers/revoke").status_code == 401


# ---------------------------------------------------------------------------
# Review follow-ups: ageing, pruning, the registration race, event streams
# ---------------------------------------------------------------------------


def test_cutoffs_older_than_the_ttl_age_out() -> None:
    from provide.uterm.server.authorization import SHARE_VIEWER_REVOCATION_TTL_S

    service = AuthorizationService()
    service.revoke_share_viewers("old", now=1000.0)
    service.revoke_share_viewers("edge", now=2000.0)
    service.revoke_share_viewers("new", now=2000.0 + SHARE_VIEWER_REVOCATION_TTL_S)
    assert service.share_viewer_revocation("old") is None
    # Exactly TTL old is kept; only strictly older cutoffs go.
    assert service.share_viewer_revocation("edge") == 2000.0
    assert service.share_viewer_revocation("new") == 2000.0 + SHARE_VIEWER_REVOCATION_TTL_S
    assert SHARE_VIEWER_REVOCATION_TTL_S == 86_400


def test_forgetting_a_session_drops_only_its_cutoff() -> None:
    service = AuthorizationService()
    service.revoke_share_viewers("s1", now=1.0)
    service.revoke_share_viewers("s2", now=2.0)
    service.forget_share_viewers("s1")
    service.forget_share_viewers("never-revoked")
    assert service.share_viewer_revocation("s1") is None
    assert service.share_viewer_revocation("s2") == 2.0


def test_a_socket_is_judged_by_its_own_app_and_principal() -> None:
    from provide.uterm.server.viewer_revocation import socket_share_viewer_revoked

    service = AuthorizationService()
    service.revoke_share_viewers("s1", now=500.0)
    app = SimpleNamespace(state=SimpleNamespace(uterm_authz=service))
    revoked = SimpleNamespace(scope={"app": app}, state=SimpleNamespace(uterm_principal=_viewer("s1", iat=100)))
    fresh = SimpleNamespace(scope={"app": app}, state=SimpleNamespace(uterm_principal=_viewer("s1", iat=900)))
    bare_hub = SimpleNamespace(scope={"app": SimpleNamespace(state=SimpleNamespace())}, state=revoked.state)
    # A hub mounted with no app in its scope (the quota tests' harness).
    no_app = SimpleNamespace(scope={}, state=revoked.state)
    # Stand-in sockets (the hub's own unit tests use MagicMock) are never revoked.
    from unittest.mock import MagicMock

    stand_in = MagicMock()
    stand_in.scope = {"app": MagicMock()}
    assert socket_share_viewer_revoked(MagicMock(), "s1") is False
    assert socket_share_viewer_revoked(stand_in, "s1") is False
    assert socket_share_viewer_revoked(revoked, "s1") is True
    assert socket_share_viewer_revoked(fresh, "s1") is False
    assert socket_share_viewer_revoked(bare_hub, "s1") is False
    assert socket_share_viewer_revoked(no_app, "s1") is False


async def _items(n: int, gate: asyncio.Event | None = None) -> Any:
    for index in range(n):
        yield f"item-{index}"
    if gate is not None:
        await gate.wait()


async def test_a_stream_that_is_not_a_share_viewers_passes_straight_through() -> None:
    from provide.uterm.server.viewer_revocation import ShareViewerStreams, stream_until_revoked

    service = AuthorizationService()
    streams = ShareViewerStreams()
    alice = Principal(subject_id="alice", roles=frozenset({"viewer"}))
    out = [item async for item in stream_until_revoked(_items(2), streams, service, alice, "s1")]
    assert out == ["item-0", "item-1"]
    assert streams.revoke(service, "s1") == 0


async def test_a_share_viewer_stream_ends_when_its_session_is_revoked() -> None:
    from provide.uterm.server.viewer_revocation import (
        STREAM_REVOKED_EVENT,
        ShareViewerStreams,
        stream_until_revoked,
    )

    service = AuthorizationService()
    streams = ShareViewerStreams()
    gate = asyncio.Event()
    watcher = _viewer("s1", iat=100)
    newcomer = _viewer("s1", iat=10_000)
    stream = stream_until_revoked(_items(1, gate), streams, service, watcher, "s1")
    other = stream_until_revoked(_items(1, gate), streams, service, newcomer, "s1")
    assert await stream.__anext__() == "item-0"
    assert await other.__anext__() == "item-0"
    pending = asyncio.ensure_future(stream.__anext__())
    pending_other = asyncio.ensure_future(other.__anext__())
    await asyncio.sleep(0)

    service.revoke_share_viewers("s1", now=500.0)
    assert streams.revoke(service, "s1") == 1
    assert await pending == STREAM_REVOKED_EVENT
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
    # The newcomer's token postdates the cutoff: still streaming.
    assert not pending_other.done()
    gate.set()
    with pytest.raises(StopAsyncIteration):
        await pending_other
    assert streams.revoke(service, "s1") == 0  # both unregistered


async def test_a_share_viewer_stream_opened_after_the_cutoff_with_an_old_token_ends_at_once() -> None:
    from provide.uterm.server.viewer_revocation import (
        STREAM_REVOKED_EVENT,
        ShareViewerStreams,
        stream_until_revoked,
    )

    service = AuthorizationService()
    service.revoke_share_viewers("s1", now=500.0)
    out = [
        item
        async for item in stream_until_revoked(_items(3), ShareViewerStreams(), service, _viewer("s1", iat=1), "s1")
    ]
    assert out == [STREAM_REVOKED_EVENT]


async def test_a_share_viewer_stream_that_runs_dry_ends_normally() -> None:
    from provide.uterm.server.viewer_revocation import ShareViewerStreams, stream_until_revoked

    service = AuthorizationService()
    streams = ShareViewerStreams()
    out = [item async for item in stream_until_revoked(_items(2), streams, service, _viewer("s1", iat=1), "s1")]
    assert out == ["item-0", "item-1"]


def test_the_app_keeps_one_stream_registry() -> None:
    from provide.uterm.server.viewer_revocation import share_viewer_streams

    app = SimpleNamespace(state=SimpleNamespace())
    first = share_viewer_streams(app)
    assert share_viewer_streams(app) is first


def test_a_socket_registered_after_the_revoke_scan_is_closed_on_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    """The race: role resolved before the cutoff, registration after the revoke's scan."""
    app = _app()
    watcher_token = _token(f"share:{_SESSION}:blackbetty:7", ["viewer"])
    hub = app.state.uterm_hub
    real_register = hub.register_browser

    async def register_after_revoke(worker_id: str, ws: Any, role: str, **kwargs: Any) -> Any:
        app.state.uterm_authz.revoke_share_viewers(worker_id)
        return await real_register(worker_id, ws, role, **kwargs)

    monkeypatch.setattr(hub, "register_browser", register_after_revoke)
    with (
        TestClient(app) as client,
        connect_test_ws(client, f"/ws/browser/{_SESSION}/term", headers=_bearer(watcher_token)) as watcher,
    ):
        closed = _close_of(watcher)
    assert closed.code == VIEWER_REVOKED_CLOSE_CODE
    assert closed.reason == VIEWER_REVOKED_REASON


def test_an_admin_registered_after_a_revoke_is_not_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app()
    hub = app.state.uterm_hub
    real_register = hub.register_browser

    async def register_after_revoke(worker_id: str, ws: Any, role: str, **kwargs: Any) -> Any:
        app.state.uterm_authz.revoke_share_viewers(worker_id)
        return await real_register(worker_id, ws, role, **kwargs)

    monkeypatch.setattr(hub, "register_browser", register_after_revoke)
    with (
        TestClient(app) as client,
        connect_test_ws(client, f"/ws/browser/{_SESSION}/term", headers=_bearer(_token("ops", ["admin"]))) as admin,
    ):
        assert _wait_for(admin, "hello")["role"] == "admin"


def test_other_owners_and_admins_scoped_elsewhere_cannot_revoke() -> None:
    app = _app()
    path = f"/api/sessions/{_SESSION}/viewers/revoke"
    with TestClient(app) as client:
        admin = _bearer(_token("ops", ["admin"]))
        patched = client.patch(f"/api/sessions/{_SESSION}", json={"owner": "owner1"}, headers=admin)
        assert patched.status_code == 200
        assert client.post(path, headers=_bearer(_token("owner2", ["operator"]))).status_code == 403
        scoped_elsewhere = jwt.encode(
            {
                "sub": "scoped",
                "roles": ["admin"],
                "admin_session_scope": "some-other-session",
                "iss": "provide-uterm",
                "aud": "provide-uterm-server",
                "iat": int(time.time()),
                "exp": int(time.time()) + 600,
            },
            key=_KEY,
            algorithm="HS256",
        )
        assert client.post(path, headers=_bearer(scoped_elsewhere)).status_code == 403
        assert client.post(path, headers=_bearer(_token("owner1", ["operator"]))).status_code == 200


def test_the_owner_and_the_hijack_holder_keep_their_sockets_through_a_revoke() -> None:
    app = _app()
    path = f"/api/sessions/{_SESSION}/viewers/revoke"
    admin_token = _token("ops", ["admin"])
    owner_token = _token("owner1", ["operator"])
    with (
        TestClient(app) as client,
        connect_test_ws(
            client,
            f"/ws/worker/{_SESSION}/term",
            headers=_bearer(app.state.uterm_config.auth.worker_bearer_token),
        ) as worker,
    ):
        assert worker.receive_json()["type"] == "snapshot_req"
        patched = client.patch(f"/api/sessions/{_SESSION}", json={"owner": "owner1"}, headers=_bearer(admin_token))
        assert patched.status_code == 200
        with (
            connect_test_ws(client, f"/ws/browser/{_SESSION}/term", headers=_bearer(admin_token)) as holder,
            connect_test_ws(client, f"/ws/browser/{_SESSION}/term", headers=_bearer(owner_token)) as owner,
        ):
            _wait_for(holder, "hello")
            assert _wait_for(owner, "hello")["role"] == "operator"
            holder.send_json({"type": "hijack_request"})
            while _wait_for(holder, "hijack_state").get("owner") != "me":
                pass
            assert client.post(path, headers=_bearer(owner_token)).json()["closed"] == 0
            for keeper in (holder, owner):
                keeper.send_json({"type": "ping"})
                assert _wait_for(keeper, "pong")["type"] == "pong"


async def test_a_revoked_registration_is_released_and_refused() -> None:
    from fastapi import WebSocketException

    from provide.uterm.server.viewer_revocation import refuse_revoked_registration

    service = AuthorizationService()
    service.revoke_share_viewers("s1", now=500.0)
    app = SimpleNamespace(state=SimpleNamespace(uterm_authz=service))
    cleaned: list[tuple[str, Any, bool]] = []

    class _Hub:
        async def cleanup_browser_disconnect(self, worker_id: str, ws: Any, owned: bool) -> None:
            cleaned.append((worker_id, ws, owned))

    revoked = SimpleNamespace(scope={"app": app}, state=SimpleNamespace(uterm_principal=_viewer("s1", iat=100)))
    fresh = SimpleNamespace(scope={"app": app}, state=SimpleNamespace(uterm_principal=_viewer("s1", iat=900)))
    await refuse_revoked_registration(_Hub(), "s1", fresh)
    assert cleaned == []
    with pytest.raises(WebSocketException) as caught:
        await refuse_revoked_registration(_Hub(), "s1", revoked)
    assert (caught.value.code, caught.value.reason) == (VIEWER_REVOKED_CLOSE_CODE, VIEWER_REVOKED_REASON)
    assert cleaned == [("s1", revoked, False)]


def test_a_live_share_viewer_event_stream_ends_on_revoke() -> None:
    import threading

    from provide.uterm.server.viewer_revocation import share_viewer_streams

    app = _app()
    watcher_token = _token(f"share:{_SESSION}:blackbetty:7", ["viewer"])
    admin = _bearer(_token("ops", ["admin"]))
    with TestClient(app) as client:
        revoked: list[int] = []

        def revoke_soon() -> None:
            streams = share_viewer_streams(app)
            deadline = time.monotonic() + 10
            while not streams._open and time.monotonic() < deadline:
                time.sleep(0.01)
            revoked.append(client.post(f"/api/sessions/{_SESSION}/viewers/revoke", headers=admin).json()["closed"])
            if revoked != [1]:
                # The revoke failed to end it: end it another way so the test
                # fails on its assertions rather than hanging.
                client.portal.call(app.state.uterm_hub.event_bus.close_worker, _SESSION)

        # Started first: the test client hands back a streamed body only once the
        # app has finished it, and only the revoke can finish this one.
        thread = threading.Thread(target=revoke_soon)
        thread.start()
        with client.stream(
            "GET", f"/api/sessions/{_SESSION}/events/stream", headers=_bearer(watcher_token)
        ) as response:
            assert response.status_code == 200
            lines = [line for line in response.iter_lines() if line]
        thread.join()
    assert lines[-1] == 'data: {"type": "viewer_access_revoked", "reason": "viewer access revoked"}'
    assert revoked == [1]


def test_tunnel_viewers_are_revoked_by_rotation_not_by_the_viewer_cutoff() -> None:
    """Revoke, then rotate: the new tunnel share link is admitted, the old one is not."""
    from urllib.parse import urlparse

    app = _app()
    owner = _bearer(_token("alice", ["operator"]))

    def cookie_for(client: TestClient, share_url: str) -> str:
        parsed = urlparse(share_url)
        landed = client.get(f"{parsed.path}?{parsed.query}", follow_redirects=False)
        assert landed.status_code == 302
        return landed.headers["set-cookie"].split(";", 1)[0]

    with TestClient(app) as client:
        created = client.post("/api/tunnels", json={"tunnel_type": "terminal"}, headers=owner).json()
        tunnel_id = created["tunnel_id"]
        old_cookie = cookie_for(client, created["share_url"])
        client.cookies.clear()

        def read(cookie: str) -> int:
            return client.get(f"/api/sessions/{tunnel_id}", headers={"Cookie": cookie}).status_code

        assert read(old_cookie) == 200
        revoked = client.post(f"/api/sessions/{tunnel_id}/viewers/revoke", headers=owner)
        assert revoked.status_code == 200
        # Out of scope for the viewer cutoff: a tunnel viewer reads on.
        assert read(old_cookie) == 200

        rotated = client.post(f"/api/tunnels/{tunnel_id}/tokens/rotate", headers=owner).json()
        new_cookie = cookie_for(client, rotated["share_url"])
        client.cookies.clear()
        assert read(new_cookie) == 200
        assert read(old_cookie) == 401


def test_a_long_poll_in_flight_when_its_viewer_is_revoked_answers_403() -> None:
    import threading

    app = _app()
    watcher_token = _token(f"share:{_SESSION}:blackbetty:7", ["viewer"])
    admin = _bearer(_token("ops", ["admin"]))
    with TestClient(app) as client:
        polled: list[Any] = []

        def poll() -> None:
            polled.append(
                client.get(f"/api/sessions/{_SESSION}/events/watch?timeout_ms=1500", headers=_bearer(watcher_token))
            )

        thread = threading.Thread(target=poll)
        thread.start()
        time.sleep(0.3)
        assert client.post(f"/api/sessions/{_SESSION}/viewers/revoke", headers=admin).status_code == 200
        thread.join()
        assert polled[0].status_code == 403
        assert polled[0].json() == {"detail": "insufficient privileges"}
        # Anyone else's long-poll is answered as before.
        plain = client.get(f"/api/sessions/{_SESSION}/events/watch?timeout_ms=100", headers=admin)
        assert plain.status_code == 200
        assert plain.json()["timed_out"] is True


async def test_the_watch_guard_passes_other_traffic_through_untouched() -> None:
    from provide.uterm.server.viewer_revocation import ShareViewerWatchGuard

    sent: list[dict[str, Any]] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def collect(message: dict[str, Any]) -> None:
        sent.append(message)

    guard = ShareViewerWatchGuard(inner)
    service = AuthorizationService()
    service.revoke_share_viewers("s1", now=500.0)
    app = SimpleNamespace(state=SimpleNamespace(uterm_authz=service))
    revoked_state = {"uterm_principal": _viewer("s1", iat=100)}
    for scope in (
        {"type": "websocket", "path": "/api/sessions/s1/events/watch"},
        {"type": "http", "method": "GET", "path": "/api/sessions/s1/events", "app": app, "state": revoked_state},
        {"type": "http", "method": "POST", "path": "/api/sessions/s1/events/watch", "app": app, "state": revoked_state},
        {"type": "http", "method": "GET", "path": "/api/sessions/s1/events/watch", "app": app, "state": {}},
        {"type": "http", "method": "GET", "path": "/api/sessions/s1/events/watch", "state": revoked_state},
        {
            "type": "http",
            "method": "GET",
            "path": "/api/sessions/s1/events/watch",
            "app": app,
            "state": {"uterm_principal": _viewer("s1", iat=900)},
        },
    ):
        sent.clear()
        await guard(scope, None, collect)
        assert [m.get("status", m.get("body")) for m in sent] == [200, b"ok"], scope
