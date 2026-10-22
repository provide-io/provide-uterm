#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""GET /api/previous-sessions[/{id}]: the capture sessions that have ended, read-only.

A previous session is history, read by fewer than the live one was: its owner
and admins always, operators unless it was private, and nobody else, a share
viewer least of all. An id nobody kept is 404, and one the caller may not read
is 403, as the live session's read answers. What is returned is redacted by
the output policy, as the live screen is. Changing its owner is
test_previous_sessions_update.py's.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from provide.uterm.server.auth import Principal
from provide.uterm.server.authorization import AuthorizationService, LocalAuthorizationProvider
from provide.uterm.server.bridge.hub.ext import RedactionRule
from provide.uterm.server.capture_state import CaptureStateStore
from provide.uterm.server.config_schema_session import SessionDefinition
from provide.uterm.server.routes.health import (
    PERSIST_CAPTURE_FEATURE,
    PREVIOUS_OWNER_FEATURE,
    PREVIOUS_SESSIONS_FEATURE,
    server_features,
)
from provide.uterm.server.routes.previous_sessions import register_previous_session_routes

from .previous_sessions_helpers import (
    BOBS,
    OPERATOR,
    PRIVATE,
    PUBLIC,
    PolicyRoles,
    as_caller,
    capture_definition,
    listed_ids,
    make_app,
    seeded_client,
)
from .viewer_revocation_helpers import bearer, mint_token


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    yield from seeded_client(tmp_path)


def test_the_owner_lists_their_own_newest_first(client: TestClient) -> None:
    assert listed_ids(client, as_caller("alice")) == [OPERATOR, PRIVATE]


def test_an_operator_lists_every_one_that_was_not_private(client: TestClient) -> None:
    assert listed_ids(client, as_caller("carol", "operator")) == [PUBLIC, OPERATOR]


def test_another_caller_lists_none_of_them_not_even_a_public_one(client: TestClient) -> None:
    assert listed_ids(client, as_caller("dave")) == []


def test_an_admin_lists_every_one(client: TestClient) -> None:
    assert listed_ids(client, as_caller("root", "admin")) == [PUBLIC, BOBS, OPERATOR, PRIVATE]


def test_an_admin_of_one_session_lists_only_that_one(client: TestClient) -> None:
    headers = bearer(mint_token("scoped", ["admin"], admin_session_scope=PRIVATE))
    assert listed_ids(client, headers) == [PRIVATE]


@pytest.mark.parametrize(
    "subject",
    [f"share:{PRIVATE}:blackbetty:7", f"share:{PRIVATE}:viewer", f"share:{PUBLIC}:blackbetty:7"],
)
def test_a_share_viewer_lists_none_and_reads_none(client: TestClient, subject: str) -> None:
    for roles in (["viewer"], ["operator"], ["admin"]):
        headers = bearer(mint_token(subject, roles))
        assert listed_ids(client, headers) == []
        for session_id in (PRIVATE, PUBLIC):
            response = client.get(f"/api/previous-sessions/{session_id}", headers=headers)
            assert (response.status_code, response.json()) == (403, {"detail": "insufficient privileges"})


def test_a_revoked_share_viewer_is_refused_too(client: TestClient) -> None:
    subject = f"share:{PRIVATE}:blackbetty:7"
    headers = bearer(mint_token(subject, ["viewer"]))
    cast("Any", client.app).state.uterm_authz.revoke_share_viewer_subject(PRIVATE, subject)
    response = client.get(f"/api/previous-sessions/{PRIVATE}", headers=headers)
    assert (response.status_code, response.json()) == (403, {"detail": "insufficient privileges"})


def test_a_listing_carries_the_summary_fields_only(client: TestClient) -> None:
    entries = client.get("/api/previous-sessions", headers=as_caller("bob")).json()["sessions"]
    [entry] = [e for e in entries if e["id"] == BOBS]
    assert set(entry) == {"id", "display_name", "owner", "ended_at", "cols", "rows"}
    assert (entry["id"], entry["display_name"], entry["owner"], entry["cols"], entry["rows"]) == (
        BOBS,
        "bob (pts/1) [live]",
        "bob",
        90,
        30,
    )
    assert isinstance(entry["ended_at"], float)


def test_the_owner_reads_the_screen_and_scrollback(client: TestClient) -> None:
    response = client.get(f"/api/previous-sessions/{PRIVATE}", headers=as_caller("alice"))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"id", "display_name", "owner", "ended_at", "cols", "rows", "screen", "scrollback"}
    assert (body["id"], body["screen"], body["scrollback"]) == (PRIVATE, "alice's private screen", ["one", "two"])


def test_an_operator_reads_an_operator_session(client: TestClient) -> None:
    response = client.get(f"/api/previous-sessions/{OPERATOR}", headers=as_caller("carol", "operator"))
    assert response.status_code == 200
    assert response.json()["screen"] == "alice's operator screen"


# The caller as (subject, roles): the token is minted in the test, never at
# collection, where a long run would see it expire before it is used.
@pytest.mark.parametrize(
    ("session_id", "caller"),
    [
        (PRIVATE, ("carol", "operator")),
        (PRIVATE, ("dave",)),
        (OPERATOR, ("dave",)),
        (BOBS, ("alice",)),
        (PUBLIC, ("dave",)),
        (PUBLIC, ("alice",)),
    ],
)
def test_a_session_the_caller_could_not_read_is_403(
    client: TestClient, session_id: str, caller: tuple[str, ...]
) -> None:
    response = client.get(f"/api/previous-sessions/{session_id}", headers=as_caller(*caller))
    assert (response.status_code, response.json()) == (403, {"detail": "insufficient privileges"})


def test_admin_is_the_role_the_provider_gives(client: TestClient) -> None:
    cast("Any", client.app).state.uterm_authz._provider = PolicyRoles()
    assert listed_ids(client, as_caller("policy-admin")) == [PUBLIC, BOBS, OPERATOR, PRIVATE]
    assert client.get(f"/api/previous-sessions/{PRIVATE}", headers=as_caller("policy-admin")).status_code == 200
    assert listed_ids(client, as_caller("root", "admin")) == []
    assert client.get(f"/api/previous-sessions/{PRIVATE}", headers=as_caller("root", "admin")).status_code == 403


_STANDING = ("is_admin", "has_capability", "has_role", "resolve_browser_role")


def _calls_while_listing(client: TestClient, headers: dict[str, str]) -> Counter[str]:
    calls: Counter[str] = Counter()
    patches = []
    for name in (*_STANDING, "can_read_session", "is_owner"):
        real = getattr(AuthorizationService, name)

        async def counted(self: Any, *args: Any, _real: Any = real, _name: str = name) -> Any:
            calls[_name] += 1
            return await _real(self, *args)

        patches.append(patch.object(AuthorizationService, name, counted))
    for one in patches:
        one.start()
    try:
        listed_ids(client, headers)
    finally:
        for one in patches:
            one.stop()
    return calls


def test_a_listing_asks_for_the_callers_standing_once_not_per_record(client: TestClient) -> None:
    # Under a webhook provider every call is a round trip: what the caller is
    # (admin, operator) is asked once a request; only the live read's own
    # per-session gate runs per record.
    for headers in (as_caller("root", "admin"), as_caller("carol", "operator"), as_caller("dave")):
        calls = _calls_while_listing(client, headers)
        assert sum(calls[name] for name in _STANDING) <= 3, (headers, calls)
        assert calls["can_read_session"] == 4, (headers, calls)


class _Everyone(LocalAuthorizationProvider):
    """A provider whose live read admits everyone, and that grants take-over to *takers*."""

    def __init__(self, *takers: str) -> None:
        self.takers = takers

    async def can_read_session(self, principal: Principal, session: SessionDefinition) -> bool:
        return True

    async def has_capability(self, principal: Principal, capability: str) -> bool:
        if capability == "session.control.hijack":
            return principal.subject_id in self.takers
        return await super().has_capability(principal, capability)  # type: ignore[arg-type]


def test_an_operator_never_reads_a_private_one_whatever_the_live_read_allows(client: TestClient) -> None:
    cast("Any", client.app).state.uterm_authz._provider = _Everyone()
    assert client.get(f"/api/previous-sessions/{PRIVATE}", headers=as_caller("carol", "operator")).status_code == 403
    assert client.get(f"/api/previous-sessions/{OPERATOR}", headers=as_caller("carol", "operator")).status_code == 200


def test_a_session_scope_makes_an_admin_only_of_an_admin(client: TestClient) -> None:
    # A viewer whose token names a session, and who may take it over: still no admin.
    cast("Any", client.app).state.uterm_authz._provider = _Everyone("scoped")
    headers = bearer(mint_token("scoped", ["viewer"], admin_session_scope=PRIVATE))
    assert client.get(f"/api/previous-sessions/{PRIVATE}", headers=headers).status_code == 403


def test_a_scoped_admin_that_may_not_take_over_is_no_admin(client: TestClient) -> None:
    cast("Any", client.app).state.uterm_authz._provider = _Everyone()
    headers = bearer(mint_token("scoped", ["admin"], admin_session_scope=PRIVATE))
    assert client.get(f"/api/previous-sessions/{PRIVATE}", headers=headers).status_code == 403
    cast("Any", client.app).state.uterm_authz._provider = _Everyone("scoped")
    assert client.get(f"/api/previous-sessions/{PRIVATE}", headers=headers).status_code == 200


def test_the_routes_are_named_gets_and_a_patch(client: TestClient) -> None:
    app = cast("Any", client.app)
    assert app.url_path_for("previous_sessions.list") == "/api/previous-sessions"
    assert app.url_path_for("previous_sessions.get", session_id=PRIVATE) == f"/api/previous-sessions/{PRIVATE}"
    router = APIRouter()
    register_previous_session_routes(router)
    assert app.url_path_for("previous_sessions.update", session_id=PRIVATE) == f"/api/previous-sessions/{PRIVATE}"
    assert [(route.name, route.methods) for route in cast("Any", router.routes)] == [
        ("previous_sessions.list", {"GET"}),
        ("previous_sessions.get", {"GET"}),
        ("previous_sessions.update", {"PATCH"}),
    ]


def test_one_kept_day_is_a_previous_sessions_feature() -> None:
    config = SimpleNamespace(session_persistence=SimpleNamespace(persist_capture=False, previous_keep_days=1))
    assert PREVIOUS_SESSIONS_FEATURE in server_features(config)
    assert PREVIOUS_OWNER_FEATURE in server_features(config)


def test_an_unknown_id_is_404(client: TestClient) -> None:
    response = client.get("/api/previous-sessions/pam-alice-capture-9-9", headers=as_caller("root", "admin"))
    assert (response.status_code, response.json()) == (
        404,
        {"detail": "unknown previous session: pam-alice-capture-9-9"},
    )


def test_a_file_gone_since_it_was_listed_is_404(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "capture-sessions" / "previous" / f"{PRIVATE}.json").unlink()
    response = client.get(f"/api/previous-sessions/{PRIVATE}", headers=as_caller("alice"))
    assert (response.status_code, response.json()) == (404, {"detail": f"unknown previous session: {PRIVATE}"})


def test_an_unauthenticated_caller_is_refused(client: TestClient) -> None:
    assert client.get("/api/previous-sessions").status_code == 401
    assert client.get(f"/api/previous-sessions/{PRIVATE}").status_code == 401


def test_a_path_unsafe_id_is_refused(client: TestClient) -> None:
    assert client.get("/api/previous-sessions/a.b", headers=as_caller("root", "admin")).status_code == 422


def test_previous_sessions_off_lists_none_and_reads_none(tmp_path: Path) -> None:
    with TestClient(make_app(tmp_path, keep_days=0, persist=True)) as off:
        assert listed_ids(off, as_caller("root", "admin")) == []
        response = off.get(f"/api/previous-sessions/{PRIVATE}", headers=as_caller("root", "admin"))
        assert response.status_code == 404
        assert PREVIOUS_SESSIONS_FEATURE not in off.get("/api/health").json()["features"]


def test_neither_feature_lists_none(tmp_path: Path) -> None:
    with TestClient(make_app(tmp_path, keep_days=0)) as off:
        assert listed_ids(off, as_caller("root", "admin")) == []
        # No store at all: an id is unknown, not an error.
        response = off.get(f"/api/previous-sessions/{PRIVATE}", headers=as_caller("root", "admin"))
        assert (response.status_code, response.json()) == (404, {"detail": f"unknown previous session: {PRIVATE}"})
        assert off.get("/api/health").json()["features"] == ["viewers.revoke.subject"]


@pytest.mark.parametrize(
    ("persist", "keep_days", "features"),
    [
        (False, 0, ["viewers.revoke.subject"]),
        (True, 0, ["viewers.revoke.subject", PERSIST_CAPTURE_FEATURE]),
        (False, 7, ["viewers.revoke.subject", PREVIOUS_SESSIONS_FEATURE, PREVIOUS_OWNER_FEATURE]),
        (
            True,
            7,
            ["viewers.revoke.subject", PERSIST_CAPTURE_FEATURE, PREVIOUS_SESSIONS_FEATURE, PREVIOUS_OWNER_FEATURE],
        ),
    ],
)
def test_health_lists_each_feature_only_when_it_is_on(
    tmp_path: Path, persist: bool, keep_days: int, features: list[str]
) -> None:
    with TestClient(make_app(tmp_path, keep_days=keep_days, persist=persist)) as app:
        assert app.get("/api/health").json()["features"] == features


def test_the_feature_names_are_pinned() -> None:
    assert (PERSIST_CAPTURE_FEATURE, PREVIOUS_SESSIONS_FEATURE, PREVIOUS_OWNER_FEATURE) == (
        "sessions.capture.persist",
        "sessions.previous",
        "sessions.previous.owner",
    )


def test_features_are_the_base_ones_without_a_config() -> None:
    assert server_features(None) == ["viewers.revoke.subject"]


def test_the_server_restores_a_live_capture_session_at_startup(tmp_path: Path) -> None:
    store = CaptureStateStore(tmp_path / "capture-sessions", persist=True, keep_days=7, scrollback_lines=100)
    definition = capture_definition(PRIVATE, "alice", "private").model_copy(update={"auto_start": False})

    async def seed() -> None:
        store.track(definition, "i")
        await store.save_definition(PRIVATE)

    asyncio.run(seed())
    with (
        patch("provide.uterm.server.pam_integration._process_start_ticks", return_value=10),
        TestClient(make_app(tmp_path, persist=True)) as app,
    ):
        response = app.get(f"/api/sessions/{PRIVATE}", headers=as_caller("alice"))
    assert response.status_code == 200
    assert (response.json()["owner"], response.json()["visibility"]) == ("alice", "private")


class _Gate:
    """An output policy that hides "secret" from viewers and fails when told to."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.asked = 0

    async def get_redaction_rules(self, context: Any) -> list[RedactionRule]:
        self.asked += 1
        # Asked about the ended session itself, as the live screen's policy is.
        assert (context.worker_id, context.action) == (PRIVATE, "output")
        if self.fail:
            raise RuntimeError("policy service down")
        return [RedactionRule(pattern="secret", replacement="[REDACTED]")] if context.role == "viewer" else []


def _gated(tmp_path: Path, gate: _Gate) -> Iterator[TestClient]:
    store = CaptureStateStore(tmp_path / "capture-sessions", persist=False, keep_days=7, scrollback_lines=100)

    async def seed() -> None:
        store.track(capture_definition(PRIVATE, "alice", "private"), "i")
        await store.save_screen(PRIVATE, {"screen": "a secret here", "cols": 80, "rows": 24}, ["secret one", "two"])
        await store.end(PRIVATE)

    asyncio.run(seed())
    app = make_app(tmp_path)
    app.state.uterm_hub._output_policy_gate = gate
    with TestClient(app) as test_client:
        yield test_client


def test_the_screen_and_scrollback_are_redacted_as_the_live_screen_is(tmp_path: Path) -> None:
    gate = _Gate()
    for client in _gated(tmp_path, gate):
        viewer = client.get(f"/api/previous-sessions/{PRIVATE}", headers=as_caller("alice")).json()
        admin = client.get(f"/api/previous-sessions/{PRIVATE}", headers=as_caller("root", "admin")).json()
    assert (viewer["screen"], viewer["scrollback"]) == ("a [REDACTED] here", ["[REDACTED] one", "two"])
    assert (admin["screen"], admin["scrollback"]) == ("a secret here", ["secret one", "two"])
    # Once a read, for the screen and every row of scrollback together.
    assert gate.asked == 2


def test_a_policy_that_cannot_answer_withholds_the_screen(tmp_path: Path) -> None:
    for client in _gated(tmp_path, _Gate(fail=True)):
        response = client.get(f"/api/previous-sessions/{PRIVATE}", headers=as_caller("alice"))
    assert (response.status_code, response.json()) == (503, {"detail": "output policy unavailable"})
