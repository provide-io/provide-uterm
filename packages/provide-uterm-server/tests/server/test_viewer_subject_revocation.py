#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Revoking one share subject of a session, not every viewer of it.

``POST /api/sessions/{id}/viewers/revoke`` with ``{"subject": "share:{id}:..."}``
records a cutoff for that subject alone: its tokens issued at or before the
cutoff are refused and its live sockets closed with 4410, while the session's
other share viewers keep watching. A token minted for the subject after the
cutoff is admitted, so the embedder can let that watcher back in. No body, or
an empty one, still revokes every share viewer of the session.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from provide.uterm.client import connect_test_ws
from provide.uterm.server.authorization import (
    SHARE_VIEWER_REVOCATION_TTL_S,
    SHARE_VIEWER_REVOKE_BODY_MAX_BYTES,
    SHARE_VIEWER_SUBJECT_MAX_BYTES,
    SHARE_VIEWER_SUBJECT_REVOCATIONS_MAX,
    SHARE_VIEWER_SUBJECT_REVOCATIONS_PER_SESSION_MAX,
    AuthorizationService,
    ShareViewerRevocationsFullError,
    ShareViewerSubjectRevocationsFullError,
)
from provide.uterm.server.bridge.identity import Principal
from provide.uterm.server.viewer_revocation import VIEWER_REVOKED_CLOSE_CODE, VIEWER_REVOKED_REASON

from .viewer_revocation_helpers import SESSION, bearer, close_of, make_app, mint_token, wait_for

_PATH = f"/api/sessions/{SESSION}/viewers/revoke"
_ALICE = f"share:{SESSION}:blackbetty:7"
_BOB = f"share:{SESSION}:blackbetty:8"


def _share(subject: str, iat: object = 100) -> Principal:
    return Principal(subject_id=subject, roles=frozenset({"viewer"}), claims={"iat": iat})


# ---------------------------------------------------------------------------
# The subject cutoff table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("iat", "revoked"), [(499, True), (500, True), (500.5, True), (501, False)])
def test_a_subject_cutoff_refuses_that_subjects_tokens_up_to_it(iat: float, revoked: bool) -> None:
    service = AuthorizationService()
    assert service.revoke_share_viewer_subject("s1", "share:s1:a", now=500.5) == 500.5
    assert service.share_viewer_subject_revocation("s1", "share:s1:a") == 500.5
    assert service.share_viewer_revoked(_share("share:s1:a", iat), "s1") is revoked


def test_a_subject_cutoff_leaves_the_sessions_other_viewers_alone() -> None:
    service = AuthorizationService()
    service.revoke_share_viewer_subject("s1", "share:s1:a", now=500.0)
    assert service.share_viewer_revoked(_share("share:s1:b", 1), "s1") is False
    assert service.share_viewer_revocation("s1") is None


def test_a_subject_cutoff_is_held_for_its_own_session_only() -> None:
    service = AuthorizationService()
    service.revoke_share_viewer_subject("s1", "share:s1:a", now=500.0)
    assert service.share_viewer_subject_revocation("s2", "share:s1:a") is None
    # Not a share viewer of s2, so no cutoff of s1's could refuse it there.
    assert service.share_viewer_revoked(_share("share:s1:a", 1), "s2") is False


def test_a_token_without_a_numeric_iat_is_refused_by_a_subject_cutoff() -> None:
    service = AuthorizationService()
    service.revoke_share_viewer_subject("s1", "share:s1:a", now=500.0)
    assert service.share_viewer_revoked(_share("share:s1:a", None), "s1") is True


def test_a_subject_cutoff_never_refuses_anyone_but_a_share_viewer() -> None:
    service = AuthorizationService()
    service.revoke_share_viewer_subject("s1", "share:s1:a", now=500.0)
    operator = Principal(subject_id="share:s1:a", roles=frozenset({"viewer", "operator"}), claims={"iat": 1})
    assert service.share_viewer_revoked(operator, "s1") is False


def test_the_later_of_the_session_and_subject_cutoffs_decides() -> None:
    service = AuthorizationService()
    service.revoke_share_viewers("s1", now=300.0)
    service.revoke_share_viewer_subject("s1", "share:s1:a", now=500.0)
    assert service.share_viewer_revoked(_share("share:s1:a", 400), "s1") is True
    assert service.share_viewer_revoked(_share("share:s1:b", 400), "s1") is False
    service.revoke_share_viewers("s1", now=600.0)
    assert service.share_viewer_revoked(_share("share:s1:a", 550), "s1") is True


def test_a_subject_revoke_defaults_to_the_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from provide.uterm.server import authorization

    monkeypatch.setattr(authorization.time, "time", lambda: 1234.5)
    assert AuthorizationService().revoke_share_viewer_subject("s1", "share:s1:a") == 1234.5


def test_a_full_session_refuses_a_new_subject_but_moves_a_held_one_forward() -> None:
    service = AuthorizationService()
    for n in range(SHARE_VIEWER_SUBJECT_REVOCATIONS_PER_SESSION_MAX):
        service.revoke_share_viewer_subject("s1", f"share:s1:{n}", now=100.0 + n)
    with pytest.raises(ShareViewerSubjectRevocationsFullError):
        service.revoke_share_viewer_subject("s1", "share:s1:new", now=500.0)
    assert service.share_viewer_subject_revocation("s1", "share:s1:new") is None
    assert service.share_viewer_subject_revocation("s1", "share:s1:0") == 100.0
    # A subject already held is moved, not added, so the cap does not refuse it.
    assert service.revoke_share_viewer_subject("s1", "share:s1:0", now=600.0) == 600.0
    # The cap is per session: another session has its own.
    assert service.revoke_share_viewer_subject("s2", "share:s2:a", now=600.0) == 600.0


def test_subject_cutoffs_older_than_the_ttl_age_out() -> None:
    service = AuthorizationService()
    service.revoke_share_viewer_subject("s1", "share:s1:a", now=10.0)
    service.revoke_share_viewer_subject("s1", "share:s1:b", now=20.0)
    # Exactly the TTL old is kept; older goes.
    service.revoke_share_viewer_subject("s2", "share:s2:a", now=20.0 + SHARE_VIEWER_REVOCATION_TTL_S)
    assert service.share_viewer_subject_revocation("s1", "share:s1:a") is None
    assert service.share_viewer_subject_revocation("s1", "share:s1:b") == 20.0
    assert service.share_viewer_revoked_sessions() == ["s1", "s2"]


def test_an_aged_out_subject_frees_its_sessions_slot() -> None:
    service = AuthorizationService()
    for n in range(SHARE_VIEWER_SUBJECT_REVOCATIONS_PER_SESSION_MAX):
        service.revoke_share_viewer_subject("s1", f"share:s1:{n}", now=float(n))
    later = 1.0 + SHARE_VIEWER_REVOCATION_TTL_S
    assert service.revoke_share_viewer_subject("s1", "share:s1:new", now=later) == later
    assert service.share_viewer_subject_revocation("s1", "share:s1:0") is None


def test_a_re_revoked_subject_ages_out_from_its_newest_cutoff() -> None:
    service = AuthorizationService()
    service.revoke_share_viewer_subject("s1", "share:s1:a", now=10.0)
    service.revoke_share_viewer_subject("s1", "share:s1:b", now=20.0)
    service.revoke_share_viewer_subject("s1", "share:s1:a", now=30.0)
    service.revoke_share_viewer_subject("s2", "share:s2:a", now=25.0 + SHARE_VIEWER_REVOCATION_TTL_S)
    assert service.share_viewer_subject_revocation("s1", "share:s1:b") is None
    assert service.share_viewer_subject_revocation("s1", "share:s1:a") == 30.0


def test_forgetting_a_session_drops_its_subject_cutoffs_only() -> None:
    service = AuthorizationService()
    service.revoke_share_viewer_subject("s1", "share:s1:a", now=10.0)
    service.revoke_share_viewer_subject("s1", "share:s1:b", now=11.0)
    service.revoke_share_viewer_subject("s2", "share:s2:a", now=12.0)
    service.revoke_share_viewers("s3", now=13.0)
    assert service.share_viewer_revoked_sessions() == ["s3", "s1", "s2"]
    service.forget_share_viewers("s1")
    assert service.share_viewer_subject_revocation("s1", "share:s1:a") is None
    assert service.share_viewer_subject_revocation("s1", "share:s1:b") is None
    assert service.share_viewer_subject_revocation("s2", "share:s2:a") == 12.0
    assert service.share_viewer_revoked_sessions() == ["s3", "s2"]
    # Its slots are free again.
    for n in range(SHARE_VIEWER_SUBJECT_REVOCATIONS_PER_SESSION_MAX):
        service.revoke_share_viewer_subject("s1", f"share:s1:{n}", now=20.0)


def test_a_session_holding_both_kinds_of_cutoff_is_listed_once() -> None:
    service = AuthorizationService()
    service.revoke_share_viewers("s1", now=10.0)
    service.revoke_share_viewer_subject("s1", "share:s1:a", now=11.0)
    assert service.share_viewer_revoked_sessions() == ["s1"]


# ---------------------------------------------------------------------------
# End to end: the REST endpoint with a subject
# ---------------------------------------------------------------------------


def test_revoking_one_subject_closes_only_its_socket_and_lets_a_fresh_token_rejoin() -> None:
    app = make_app()
    alice = mint_token(_ALICE, ["viewer"])
    bob = mint_token(_BOB, ["viewer"])
    admin = mint_token("ops", ["admin"])
    with (
        TestClient(app) as client,
        connect_test_ws(
            client, f"/ws/worker/{SESSION}/term", headers=bearer(app.state.uterm_config.auth.worker_bearer_token)
        ) as worker,
    ):
        assert worker.receive_json()["type"] == "snapshot_req"
        with (
            connect_test_ws(client, f"/ws/browser/{SESSION}/term", headers=bearer(alice)) as alice_ws,
            connect_test_ws(client, f"/ws/browser/{SESSION}/term", headers=bearer(bob)) as bob_ws,
        ):
            assert wait_for(alice_ws, "hello")["role"] == "viewer"
            assert wait_for(bob_ws, "hello")["role"] == "viewer"

            response = client.post(_PATH, headers=bearer(admin), json={"subject": _ALICE})
            assert response.status_code == 200
            body = response.json()
            assert body == {
                "session_id": SESSION,
                "revoked_at": body["revoked_at"],
                "closed": 1,
                "close_code": VIEWER_REVOKED_CLOSE_CODE,
                "close_reason": VIEWER_REVOKED_REASON,
                "subject": _ALICE,
            }
            assert isinstance(body["revoked_at"], float)

            closed = close_of(alice_ws)
            assert closed.code == VIEWER_REVOKED_CLOSE_CODE
            assert closed.reason == VIEWER_REVOKED_REASON
            bob_ws.send_json({"type": "ping"})
            assert wait_for(bob_ws, "pong")["type"] == "pong"

        assert client.get(f"/api/sessions/{SESSION}", headers=bearer(alice)).status_code == 403
        assert client.get(f"/api/sessions/{SESSION}", headers=bearer(bob)).status_code == 200
        with (
            pytest.raises(WebSocketDisconnect),
            connect_test_ws(client, f"/ws/browser/{SESSION}/term", headers=bearer(alice)) as again,
        ):
            again.receive_json()

        fresh = mint_token(_ALICE, ["viewer"], iat=int(body["revoked_at"]) + 1)
        assert client.get(f"/api/sessions/{SESSION}", headers=bearer(fresh)).status_code == 200
        with connect_test_ws(client, f"/ws/browser/{SESSION}/term", headers=bearer(fresh)) as rewatch:
            assert wait_for(rewatch, "hello")["role"] == "viewer"


@pytest.mark.parametrize("content", [b"", b"  \n", b"{}", b"null", b" null\n"])
def test_no_body_an_empty_one_null_or_no_subject_still_revokes_the_whole_session(content: bytes) -> None:
    app = make_app()
    # Minted before the revoke, so its iat cannot land in a later second than the cutoff.
    bob = mint_token(_BOB, ["viewer"])
    with TestClient(app) as client:
        response = client.post(_PATH, headers=bearer(mint_token("ops", ["admin"])), content=content)
        assert response.status_code == 200
        # A whole-session revoke keeps the original answer: no subject key.
        assert sorted(response.json()) == ["close_code", "close_reason", "closed", "revoked_at", "session_id"]
        az = app.state.uterm_authz
        assert az.share_viewer_revocation(SESSION) == response.json()["revoked_at"]
        assert client.get(f"/api/sessions/{SESSION}", headers=bearer(bob)).status_code == 403


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b'{"subject": ',
        b"[]",
        # Nested deeper than Python 3.11's json recursion limit (1000) yet under
        # the body cap: a RecursionError there, an array here, 422 either way.
        b"[" * 2000 + b"]" * 2000,
        b'"share:provide-shell:x"',
        b'{"subject": null}',
        b'{"subject": 7}',
        b'{"subject": "share:provide-shell:x", "extra": 1}',
        b'{"subjects": "share:provide-shell:x"}',
        b"\xff",
        b'{"subject": "share:provide-shell:\xff"}',
        b"{} {}",
        b"X",
        b"\xef\xbb\xbf{}",
        "{}".encode("utf-16"),
        # Only JSON's own whitespace is blank; other whitespace is malformed.
        b"\x0c",
        b"\xc2\xa0",
    ],
)
def test_a_malformed_body_is_a_422_that_revokes_nothing(content: bytes) -> None:
    app = make_app()
    with TestClient(app) as client:
        response = client.post(_PATH, headers=bearer(mint_token("ops", ["admin"])), content=content)
        assert response.status_code == 422
        assert response.json() == {"detail": "invalid request body"}
        assert app.state.uterm_authz.share_viewer_revoked_sessions() == []


@pytest.mark.parametrize(
    "subject",
    [
        "share:other-session:blackbetty:7",
        "share:provide-shellx:7",
        "share:provide-shell",
        "share:provide-shell:",
        "alice",
        "share:provide-shell:" + "x" * (SHARE_VIEWER_SUBJECT_MAX_BYTES - len("share:provide-shell:") + 1),
        "share:provide-shell:" + "é" * ((SHARE_VIEWER_SUBJECT_MAX_BYTES - len("share:provide-shell:")) // 2 + 1),
    ],
)
def test_a_subject_that_is_not_this_sessions_share_subject_is_a_422(subject: str) -> None:
    app = make_app()
    with TestClient(app) as client:
        response = client.post(_PATH, headers=bearer(mint_token("ops", ["admin"])), json={"subject": subject})
        assert response.status_code == 422
        assert response.json() == {"detail": "subject must be a share subject of this session"}
        assert app.state.uterm_authz.share_viewer_revoked_sessions() == []


def test_a_subject_of_exactly_the_longest_length_is_accepted() -> None:
    app = make_app()
    subject = "share:provide-shell:" + "x" * (SHARE_VIEWER_SUBJECT_MAX_BYTES - len("share:provide-shell:"))
    with TestClient(app) as client:
        response = client.post(_PATH, headers=bearer(mint_token("ops", ["admin"])), json={"subject": subject})
        assert response.status_code == 200
        assert app.state.uterm_authz.share_viewer_subject_revocation(SESSION, subject) == response.json()["revoked_at"]


def test_a_session_full_of_revoked_subjects_is_a_429() -> None:
    app = make_app()
    az = app.state.uterm_authz
    for n in range(SHARE_VIEWER_SUBJECT_REVOCATIONS_PER_SESSION_MAX):
        az.revoke_share_viewer_subject(SESSION, f"share:{SESSION}:{n}")
    with TestClient(app) as client:
        response = client.post(_PATH, headers=bearer(mint_token("ops", ["admin"])), json={"subject": _ALICE})
        assert response.status_code == 429
        assert response.json() == {
            "detail": "too many viewers of this session revoked one by one; revoke them all instead"
        }
        # Revoking every viewer still works.
        assert client.post(_PATH, headers=bearer(mint_token("ops", ["admin"]))).status_code == 200


def test_a_refused_caller_is_refused_before_its_body_is_judged() -> None:
    app = make_app()
    with TestClient(app) as client:
        response = client.post(_PATH, headers=bearer(mint_token("alice", ["viewer"])), content=b"not json")
        assert response.status_code == 403


@pytest.mark.parametrize(
    ("escape", "status"),
    [
        ("\\ud83d\\ude00", 200),
        ("\\\\ud800", 200),
        ("\\u0041", 200),
        ("\\ud800", 422),
        ("\\udc00", 422),
        ("\\ud800x", 422),
        ("\\ud800\\u0041", 422),
        ("\\ud800\\ud800", 422),
        ("\\ude00\\ud83d", 422),
    ],
)
def test_a_subject_spelling_a_lone_surrogate_is_malformed(escape: str, status: int) -> None:
    # JSON can spell a lone surrogate, which is not text; a pair, or an escaped
    # backslash before a "u", is.
    app = make_app()
    with TestClient(app) as client:
        content = ('{"subject": "share:provide-shell:' + escape + '"}').encode()
        response = client.post(_PATH, headers=bearer(mint_token("ops", ["admin"])), content=content)
        assert response.status_code == status
        if status == 422:
            assert response.json() == {"detail": "invalid request body"}


def test_a_whole_session_revoke_drops_the_subject_cutoffs_it_subsumes() -> None:
    service = AuthorizationService()
    service.revoke_share_viewer_subject("s1", "share:s1:a", now=100.0)
    service.revoke_share_viewer_subject("s1", "share:s1:b", now=200.0)
    service.revoke_share_viewer_subject("s1", "share:s1:c", now=300.0)
    service.revoke_share_viewer_subject("s2", "share:s2:a", now=100.0)
    service.revoke_share_viewers("s1", now=200.0)
    # At or before the session cutoff: dropped. After it: kept. Other sessions: untouched.
    assert service.share_viewer_subject_revocation("s1", "share:s1:a") is None
    assert service.share_viewer_subject_revocation("s1", "share:s1:b") is None
    assert service.share_viewer_subject_revocation("s1", "share:s1:c") == 300.0
    assert service.share_viewer_subject_revocation("s2", "share:s2:a") == 100.0
    # Nothing a dropped cutoff refused is admitted: the session cutoff refuses it.
    assert service.share_viewer_revoked(_share("share:s1:a", 150), "s1") is True
    assert service.share_viewer_revoked(_share("share:s1:c", 250), "s1") is True


def test_a_whole_session_revoke_frees_a_full_sessions_slots() -> None:
    service = AuthorizationService()
    for n in range(SHARE_VIEWER_SUBJECT_REVOCATIONS_PER_SESSION_MAX):
        service.revoke_share_viewer_subject("s1", f"share:s1:{n}", now=100.0)
    service.revoke_share_viewers("s1", now=100.0)
    assert service.revoke_share_viewer_subject("s1", "share:s1:new", now=101.0) == 101.0
    assert service.share_viewer_revoked_sessions() == ["s1"]


def test_a_refused_whole_session_revoke_drops_no_subject_cutoff() -> None:
    from provide.uterm.server.authorization import SHARE_VIEWER_REVOCATIONS_MAX

    service = AuthorizationService()
    for n in range(SHARE_VIEWER_REVOCATIONS_MAX):
        service.revoke_share_viewers(f"other-{n}", now=100.0)
    service.revoke_share_viewer_subject("s1", "share:s1:a", now=100.0)
    with pytest.raises(ShareViewerRevocationsFullError):
        service.revoke_share_viewers("s1", now=200.0)
    assert service.share_viewer_subject_revocation("s1", "share:s1:a") == 100.0


def test_a_full_subject_table_refuses_a_new_subject_but_moves_a_held_one_forward() -> None:
    service = AuthorizationService()
    per = SHARE_VIEWER_SUBJECT_REVOCATIONS_PER_SESSION_MAX
    for n in range(SHARE_VIEWER_SUBJECT_REVOCATIONS_MAX):
        service.revoke_share_viewer_subject(f"s{n // per}", f"share:s{n // per}:{n % per}", now=100.0)
    with pytest.raises(ShareViewerRevocationsFullError):
        service.revoke_share_viewer_subject("fresh", "share:fresh:a", now=200.0)
    assert service.share_viewer_subject_revocation("fresh", "share:fresh:a") is None
    assert service.revoke_share_viewer_subject("s0", "share:s0:0", now=200.0) == 200.0
    # An aged-out cutoff makes room again.
    later = 100.5 + SHARE_VIEWER_REVOCATION_TTL_S
    assert service.revoke_share_viewer_subject("fresh", "share:fresh:a", now=later) == later


def test_a_body_past_the_cap_is_a_422_and_one_at_it_is_read() -> None:
    app = make_app()
    with TestClient(app) as client:
        headers = bearer(mint_token("ops", ["admin"]))
        over = client.post(_PATH, headers=headers, content=b" " * (SHARE_VIEWER_REVOKE_BODY_MAX_BYTES - 1) + b"{}")
        assert over.status_code == 422
        assert over.json() == {"detail": "invalid request body"}
        at = client.post(_PATH, headers=headers, content=b" " * (SHARE_VIEWER_REVOKE_BODY_MAX_BYTES - 2) + b"{}")
        assert at.status_code == 200
    assert SHARE_VIEWER_REVOKE_BODY_MAX_BYTES == 4096
    assert SHARE_VIEWER_SUBJECT_REVOCATIONS_MAX == 10_000
