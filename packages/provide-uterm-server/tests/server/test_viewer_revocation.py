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


def test_the_cutoff_table_is_bounded_and_forgets_the_oldest() -> None:
    service = AuthorizationService()
    for index in range(SHARE_VIEWER_REVOCATIONS_MAX):
        service.revoke_share_viewers(f"s{index}", now=float(index))
    # Re-revoking s0 makes it the newest, so s1 is the one that falls out.
    service.revoke_share_viewers("s0", now=1.0e6)
    service.revoke_share_viewers("overflow", now=2.0e6)
    assert service.share_viewer_revocation("s1") is None
    assert service.share_viewer_revocation("s0") == 1.0e6
    assert service.share_viewer_revocation("s2") == 2.0
    assert service.share_viewer_revocation("overflow") == 2.0e6


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
    elevated = _Socket(_viewer("s1", iat=100))  # same token, but the socket holds a non-viewer role
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

    assert closed == 2
    assert watcher.closed == [(VIEWER_REVOKED_CLOSE_CODE, VIEWER_REVOKED_REASON)]
    assert broken.closed == [(VIEWER_REVOKED_CLOSE_CODE, VIEWER_REVOKED_REASON)]
    for untouched in (newcomer, elevated, stranger, unauthenticated):
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


def _close_of(browser: Any) -> WebSocketDisconnect:
    with pytest.raises(WebSocketDisconnect) as caught:
        while True:
            browser.receive_json()
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
