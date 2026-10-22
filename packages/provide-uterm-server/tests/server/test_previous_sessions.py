#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""GET /api/previous-sessions[/{id}]: the capture sessions that have ended, and PATCH of their owner.

A previous session is history, read by fewer than the live one was: its owner
and admins always, operators unless it was private, and nobody else, a share
viewer least of all. An id nobody kept is 404, and one the caller may not read
is 403, as the live session's read answers. What is returned is redacted by
the output policy, as the live screen is.

Only an admin may change who owns one, or its visibility: the live session's
rule for an owner change, since a login that ended before its owner was set
is otherwise read by nobody but an admin.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.auth import Principal
from provide.uterm.server.authorization import AuthorizationService, LocalAuthorizationProvider
from provide.uterm.server.bridge.hub.ext import RedactionRule
from provide.uterm.server.capture_state import CaptureStateStore
from provide.uterm.server.config_schema_session import SessionDefinition
from provide.uterm.server.models import AuthConfig
from provide.uterm.server.routes.health import (
    PERSIST_CAPTURE_FEATURE,
    PREVIOUS_OWNER_FEATURE,
    PREVIOUS_SESSIONS_FEATURE,
    server_features,
)
from provide.uterm.server.routes.previous_sessions import register_previous_session_routes

from .viewer_revocation_helpers import JWT_KEY, bearer, mint_token

PRIVATE = "pam-alice-capture-1-10"
OPERATOR = "pam-alice-capture-2-10"
BOBS = "pam-bob-capture-3-10"
PUBLIC = "pam-bob-capture-4-10"


def _definition(session_id: str, owner: str, visibility: str) -> SessionDefinition:
    return SessionDefinition(
        session_id=session_id,
        display_name=f"{owner} (pts/1) [live]",
        connector_type="pty_capture",
        connector_config={"socket_path": "/run/uterm-capture/s.sock"},
        owner=owner,
        visibility=visibility,
    )


async def _end(store: CaptureStateStore, definition: SessionDefinition, screen: str, ended_at: float) -> None:
    store._clock = lambda: ended_at
    store.track(definition, "i")
    await store.save_screen(definition.session_id, {"screen": screen, "cols": 90, "rows": 30}, ["one", "two"])
    await store.end(definition.session_id)


def _app(tmp_path: Path, *, keep_days: int = 7, persist: bool = False) -> Any:
    config = default_server_config()
    config.sessions = []
    config.recording.directory = tmp_path / "recordings"
    config.session_persistence.previous_keep_days = keep_days
    config.session_persistence.persist_capture = persist
    config.session_persistence.state_dir = tmp_path / "capture-sessions"
    config.auth = AuthConfig(
        mode="jwt",
        jwt_public_key_pem=JWT_KEY,
        jwt_algorithms=["HS256"],
        worker_bearer_token=mint_token("runtime-worker", ["admin"]),
        clock_skew_seconds=30,
    )
    return create_server_app(config, api_only=True)


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    store = CaptureStateStore(tmp_path / "capture-sessions", persist=False, keep_days=7, scrollback_lines=100)
    now = time.time()

    async def seed() -> None:
        await _end(store, _definition(PRIVATE, "alice", "private"), "alice's private screen", now - 30)
        await _end(store, _definition(OPERATOR, "alice", "operator"), "alice's operator screen", now - 20)
        await _end(store, _definition(BOBS, "bob", "private"), "bob's screen", now - 10)
        await _end(store, _definition(PUBLIC, "bob", "public"), "bob's public screen", now - 5)

    asyncio.run(seed())
    with TestClient(_app(tmp_path)) as test_client:
        yield test_client


def _as(sub: str, *roles: str) -> dict[str, str]:
    return bearer(mint_token(sub, list(roles) or ["viewer"]))


def _listed(client: TestClient, headers: dict[str, str]) -> list[str]:
    response = client.get("/api/previous-sessions", headers=headers)
    assert response.status_code == 200
    return [entry["id"] for entry in response.json()["sessions"]]


def test_the_owner_lists_their_own_newest_first(client: TestClient) -> None:
    assert _listed(client, _as("alice")) == [OPERATOR, PRIVATE]


def test_an_operator_lists_every_one_that_was_not_private(client: TestClient) -> None:
    assert _listed(client, _as("carol", "operator")) == [PUBLIC, OPERATOR]


def test_another_caller_lists_none_of_them_not_even_a_public_one(client: TestClient) -> None:
    assert _listed(client, _as("dave")) == []


def test_an_admin_lists_every_one(client: TestClient) -> None:
    assert _listed(client, _as("root", "admin")) == [PUBLIC, BOBS, OPERATOR, PRIVATE]


def test_an_admin_of_one_session_lists_only_that_one(client: TestClient) -> None:
    headers = bearer(mint_token("scoped", ["admin"], admin_session_scope=PRIVATE))
    assert _listed(client, headers) == [PRIVATE]


@pytest.mark.parametrize(
    "subject",
    [f"share:{PRIVATE}:blackbetty:7", f"share:{PRIVATE}:viewer", f"share:{PUBLIC}:blackbetty:7"],
)
def test_a_share_viewer_lists_none_and_reads_none(client: TestClient, subject: str) -> None:
    for roles in (["viewer"], ["operator"], ["admin"]):
        headers = bearer(mint_token(subject, roles))
        assert _listed(client, headers) == []
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
    entries = client.get("/api/previous-sessions", headers=_as("bob")).json()["sessions"]
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
    response = client.get(f"/api/previous-sessions/{PRIVATE}", headers=_as("alice"))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"id", "display_name", "owner", "ended_at", "cols", "rows", "screen", "scrollback"}
    assert (body["id"], body["screen"], body["scrollback"]) == (PRIVATE, "alice's private screen", ["one", "two"])


def test_an_operator_reads_an_operator_session(client: TestClient) -> None:
    response = client.get(f"/api/previous-sessions/{OPERATOR}", headers=_as("carol", "operator"))
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
    response = client.get(f"/api/previous-sessions/{session_id}", headers=_as(*caller))
    assert (response.status_code, response.json()) == (403, {"detail": "insufficient privileges"})


class _PolicyRoles(LocalAuthorizationProvider):
    """A provider that decides roles itself, as a webhook does: "policy-admin" is
    admin without the role in its token, and "root" is a viewer with it."""

    async def can_read_session(self, principal: Principal, session: SessionDefinition) -> bool:
        return principal.subject_id in ("policy-admin", "root")

    async def is_admin(self, principal: Principal) -> bool:
        return principal.subject_id == "policy-admin"

    async def has_capability(self, principal: Principal, capability: str) -> bool:
        return principal.subject_id == "policy-admin" or await super().has_capability(principal, capability)


def test_admin_is_the_role_the_provider_gives(client: TestClient) -> None:
    cast("Any", client.app).state.uterm_authz._provider = _PolicyRoles()
    assert _listed(client, _as("policy-admin")) == [PUBLIC, BOBS, OPERATOR, PRIVATE]
    assert client.get(f"/api/previous-sessions/{PRIVATE}", headers=_as("policy-admin")).status_code == 200
    assert _listed(client, _as("root", "admin")) == []
    assert client.get(f"/api/previous-sessions/{PRIVATE}", headers=_as("root", "admin")).status_code == 403


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
        _listed(client, headers)
    finally:
        for one in patches:
            one.stop()
    return calls


def test_a_listing_asks_for_the_callers_standing_once_not_per_record(client: TestClient) -> None:
    # Under a webhook provider every call is a round trip: what the caller is
    # (admin, operator) is asked once a request; only the live read's own
    # per-session gate runs per record.
    for headers in (_as("root", "admin"), _as("carol", "operator"), _as("dave")):
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
    assert client.get(f"/api/previous-sessions/{PRIVATE}", headers=_as("carol", "operator")).status_code == 403
    assert client.get(f"/api/previous-sessions/{OPERATOR}", headers=_as("carol", "operator")).status_code == 200


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
    response = client.get("/api/previous-sessions/pam-alice-capture-9-9", headers=_as("root", "admin"))
    assert (response.status_code, response.json()) == (
        404,
        {"detail": "unknown previous session: pam-alice-capture-9-9"},
    )


def test_a_file_gone_since_it_was_listed_is_404(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "capture-sessions" / "previous" / f"{PRIVATE}.json").unlink()
    response = client.get(f"/api/previous-sessions/{PRIVATE}", headers=_as("alice"))
    assert (response.status_code, response.json()) == (404, {"detail": f"unknown previous session: {PRIVATE}"})


def test_an_unauthenticated_caller_is_refused(client: TestClient) -> None:
    assert client.get("/api/previous-sessions").status_code == 401
    assert client.get(f"/api/previous-sessions/{PRIVATE}").status_code == 401


def test_a_path_unsafe_id_is_refused(client: TestClient) -> None:
    assert client.get("/api/previous-sessions/a.b", headers=_as("root", "admin")).status_code == 422


def test_previous_sessions_off_lists_none_and_reads_none(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path, keep_days=0, persist=True)) as off:
        assert _listed(off, _as("root", "admin")) == []
        response = off.get(f"/api/previous-sessions/{PRIVATE}", headers=_as("root", "admin"))
        assert response.status_code == 404
        assert PREVIOUS_SESSIONS_FEATURE not in off.get("/api/health").json()["features"]


def test_neither_feature_lists_none(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path, keep_days=0)) as off:
        assert _listed(off, _as("root", "admin")) == []
        # No store at all: an id is unknown, not an error.
        response = off.get(f"/api/previous-sessions/{PRIVATE}", headers=_as("root", "admin"))
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
    with TestClient(_app(tmp_path, keep_days=keep_days, persist=persist)) as app:
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
    definition = _definition(PRIVATE, "alice", "private").model_copy(update={"auto_start": False})

    async def seed() -> None:
        store.track(definition, "i")
        await store.save_definition(PRIVATE)

    asyncio.run(seed())
    with (
        patch("provide.uterm.server.pam_integration._process_start_ticks", return_value=10),
        TestClient(_app(tmp_path, persist=True)) as app,
    ):
        response = app.get(f"/api/sessions/{PRIVATE}", headers=_as("alice"))
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
        store.track(_definition(PRIVATE, "alice", "private"), "i")
        await store.save_screen(PRIVATE, {"screen": "a secret here", "cols": 80, "rows": 24}, ["secret one", "two"])
        await store.end(PRIVATE)

    asyncio.run(seed())
    app = _app(tmp_path)
    app.state.uterm_hub._output_policy_gate = gate
    with TestClient(app) as test_client:
        yield test_client


def test_the_screen_and_scrollback_are_redacted_as_the_live_screen_is(tmp_path: Path) -> None:
    gate = _Gate()
    for client in _gated(tmp_path, gate):
        viewer = client.get(f"/api/previous-sessions/{PRIVATE}", headers=_as("alice")).json()
        admin = client.get(f"/api/previous-sessions/{PRIVATE}", headers=_as("root", "admin")).json()
    assert (viewer["screen"], viewer["scrollback"]) == ("a [REDACTED] here", ["[REDACTED] one", "two"])
    assert (admin["screen"], admin["scrollback"]) == ("a secret here", ["secret one", "two"])
    # Once a read, for the screen and every row of scrollback together.
    assert gate.asked == 2


def test_a_policy_that_cannot_answer_withholds_the_screen(tmp_path: Path) -> None:
    for client in _gated(tmp_path, _Gate(fail=True)):
        response = client.get(f"/api/previous-sessions/{PRIVATE}", headers=_as("alice"))
    assert (response.status_code, response.json()) == (503, {"detail": "output policy unavailable"})


# PATCH /api/previous-sessions/{id}: who owns an ended session, and who else may read it.


def _patch(client: TestClient, session_id: str, body: Any, headers: dict[str, str]) -> Any:
    return client.patch(f"/api/previous-sessions/{session_id}", json=body, headers=headers)


def _on_disk(tmp_path: Path, session_id: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((tmp_path / "capture-sessions" / "previous" / f"{session_id}.json").read_bytes())
    return data


def _reloaded(tmp_path: Path) -> CaptureStateStore:
    store = CaptureStateStore(tmp_path / "capture-sessions", persist=False, keep_days=7, scrollback_lines=100)
    store.load_previous()
    return store


def test_an_admin_sets_the_owner_and_visibility(client: TestClient, tmp_path: Path) -> None:
    listed = {e["id"]: e for e in client.get("/api/previous-sessions", headers=_as("root", "admin")).json()["sessions"]}
    response = _patch(client, PUBLIC, {"owner": "blackbetty:7", "visibility": "private"}, _as("root", "admin"))
    assert response.status_code == 200
    # The listing's entry, as GET of the list returns it: no screen.
    assert response.json() == {**listed[PUBLIC], "owner": "blackbetty:7"}
    on_disk = _on_disk(tmp_path, PUBLIC)
    assert (on_disk["session"]["owner"], on_disk["session"]["visibility"]) == ("blackbetty:7", "private")
    # Everything else is kept, the screen and when it ended above all.
    assert (on_disk["screen"]["ansi"], on_disk["scrollback"]) == ("bob's public screen", ["one", "two"])
    assert on_disk["ended_at"] == listed[PUBLIC]["ended_at"]
    # The new owner reads it; its old owner, and an operator now that it is private, do not.
    assert _listed(client, _as("blackbetty:7")) == [PUBLIC]
    read = client.get(f"/api/previous-sessions/{PUBLIC}", headers=_as("blackbetty:7"))
    assert (read.status_code, read.json()["screen"]) == (200, "bob's public screen")
    assert _listed(client, _as("bob")) == [BOBS]
    assert _listed(client, _as("carol", "operator")) == [OPERATOR]


def test_an_owner_set_survives_a_restart(client: TestClient, tmp_path: Path) -> None:
    assert _patch(client, PRIVATE, {"owner": "blackbetty:7"}, _as("root", "admin")).status_code == 200
    store = _reloaded(tmp_path)
    kept = store.previous_summary(PRIVATE)
    assert kept is not None
    assert (kept.definition.owner, kept.definition.visibility) == ("blackbetty:7", "private")
    record = asyncio.run(store.previous_session(PRIVATE))
    assert record is not None
    assert record.screen == "alice's private screen"


def test_one_field_leaves_the_other_as_it_was(client: TestClient, tmp_path: Path) -> None:
    assert _patch(client, OPERATOR, {"visibility": "public"}, _as("root", "admin")).status_code == 200
    assert _on_disk(tmp_path, OPERATOR)["session"]["owner"] == "alice"
    assert _on_disk(tmp_path, OPERATOR)["session"]["visibility"] == "public"
    assert _listed(client, _as("carol", "operator")) == [PUBLIC, OPERATOR]
    response = _patch(client, OPERATOR, {"owner": "blackbetty:7"}, _as("root", "admin"))
    assert response.json()["owner"] == "blackbetty:7"
    assert _on_disk(tmp_path, OPERATOR)["session"]["visibility"] == "public"


def test_a_change_is_audited(client: TestClient) -> None:
    audit = MagicMock()
    with patch("provide.uterm.server.routes.previous_sessions.audit_event", audit):
        _patch(client, PRIVATE, {"owner": "blackbetty:7", "visibility": "operator"}, _as("root", "admin"))
        # A refused one is not.
        _patch(client, PRIVATE, {"owner": "blackbetty:7"}, _as("alice"))
    audit.assert_called_once_with(
        "previous_session.update",
        principal="root",
        session_id=PRIVATE,
        source_ip="testclient",
        detail={"owner": "blackbetty:7", "visibility": "operator"},
    )


# As (subject, roles), minted in the test.
@pytest.mark.parametrize(
    "caller",
    [("alice",), ("carol", "operator"), ("dave",)],
)
@pytest.mark.parametrize("body", [{"owner": "alice"}, {"visibility": "public"}, {"owner": "x", "visibility": "public"}])
def test_anyone_but_an_admin_is_refused_and_nothing_changes(
    client: TestClient, tmp_path: Path, caller: tuple[str, ...], body: dict[str, str]
) -> None:
    before = _on_disk(tmp_path, PRIVATE)
    response = _patch(client, PRIVATE, body, _as(*caller))
    assert (response.status_code, response.json()) == (403, {"detail": "admin privileges required"})
    assert _on_disk(tmp_path, PRIVATE) == before
    assert _listed(client, _as("alice")) == [OPERATOR, PRIVATE]


@pytest.mark.parametrize("subject", [f"share:{PRIVATE}:blackbetty:7", f"share:{PRIVATE}:viewer"])
def test_a_share_viewer_is_refused_whatever_its_roles(client: TestClient, tmp_path: Path, subject: str) -> None:
    before = _on_disk(tmp_path, PRIVATE)
    for roles in (["viewer"], ["operator"], ["admin"]):
        response = _patch(client, PRIVATE, {"owner": "blackbetty:7"}, bearer(mint_token(subject, roles)))
        assert (response.status_code, response.json()) == (403, {"detail": "admin privileges required"})
    assert _on_disk(tmp_path, PRIVATE) == before


def test_a_session_scoped_admin_is_refused(client: TestClient) -> None:
    headers = bearer(mint_token("scoped", ["admin"], admin_session_scope=PRIVATE))
    assert _patch(client, PRIVATE, {"owner": "scoped"}, headers).status_code == 403


class _PolicyMutator(_PolicyRoles):
    """_PolicyRoles whose live-PATCH rule admits "policy-admin" alone, recording what it is asked."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str, str]] = []

    async def can_mutate_session(self, principal: Principal, session: SessionDefinition, action: str) -> bool:
        self.asked.append((principal.subject_id, session.session_id, action))
        return principal.subject_id == "policy-admin"


def test_the_admin_is_the_one_the_provider_names(client: TestClient) -> None:
    provider = _PolicyMutator()
    cast("Any", client.app).state.uterm_authz._provider = provider
    assert _patch(client, PRIVATE, {"owner": "x"}, _as("root", "admin")).status_code == 403
    assert _patch(client, PRIVATE, {"owner": "x"}, _as("policy-admin")).status_code == 200
    # The live PATCH's own question, about the definition the session ended with.
    assert ("policy-admin", PRIVATE, "session.control.update") in provider.asked


class _NoMutation(LocalAuthorizationProvider):
    """A provider whose live-PATCH rule refuses everyone, admins included."""

    async def can_mutate_session(self, principal: Principal, session: SessionDefinition, action: str) -> bool:
        return False


def test_an_admin_the_live_patch_would_refuse_is_refused(client: TestClient, tmp_path: Path) -> None:
    cast("Any", client.app).state.uterm_authz._provider = _NoMutation()
    before = _on_disk(tmp_path, BOBS)
    response = _patch(client, BOBS, {"owner": "eve"}, _as("root", "admin"))
    assert (response.status_code, response.json()) == (403, {"detail": "admin privileges required"})
    assert _on_disk(tmp_path, BOBS) == before


def test_an_admin_token_narrowed_to_reading_cannot_take_a_session_over(client: TestClient, tmp_path: Path) -> None:
    # The review's reproduction: an admin whose token's scopes leave it only
    # session.read may not read bob's private history, so it may not make
    # itself (or anyone) its owner either.
    headers = bearer(mint_token("eve", ["admin"], scope="session.read"))
    before = _on_disk(tmp_path, BOBS)
    assert client.get(f"/api/previous-sessions/{BOBS}", headers=headers).status_code == 403
    response = _patch(client, BOBS, {"owner": "eve"}, headers)
    assert (response.status_code, response.json()) == (403, {"detail": "admin privileges required"})
    assert _on_disk(tmp_path, BOBS) == before
    assert client.get(f"/api/previous-sessions/{BOBS}", headers=headers).status_code == 403


@pytest.mark.parametrize(
    ("scopes", "status"),
    [
        # Take-over without the live PATCH's capability, and the reverse.
        (["session.control.hijack"], 403),
        (["session.control.update"], 403),
        (["graphical.target.read"], 403),
        (["session.control.hijack", "session.control.update"], 200),
        (["*"], 200),
    ],
)
def test_an_admin_needs_take_over_and_update_in_its_scopes(client: TestClient, scopes: list[str], status: int) -> None:
    headers = bearer(mint_token("root", ["admin"], scope=scopes))
    assert _patch(client, BOBS, {"owner": "blackbetty:7"}, headers).status_code == status


def test_an_admin_of_another_session_is_refused(client: TestClient, tmp_path: Path) -> None:
    before = _on_disk(tmp_path, PRIVATE)
    headers = bearer(mint_token("scoped", ["admin"], admin_session_scope=BOBS))
    assert _patch(client, PRIVATE, {"owner": "scoped"}, headers).status_code == 403
    assert _on_disk(tmp_path, PRIVATE) == before


def test_an_unscoped_admin_token_like_black_bettys_is_admitted(client: TestClient) -> None:
    # No scope claim at all: every capability its roles give.
    headers = bearer(mint_token("blackbetty-gateway", ["admin"]))
    response = _patch(client, BOBS, {"owner": "blackbetty:7", "visibility": "private"}, headers)
    assert (response.status_code, response.json()["owner"]) == (200, "blackbetty:7")


def test_patching_an_unknown_id_is_404(client: TestClient) -> None:
    response = _patch(client, "pam-alice-capture-9-9", {"owner": "x"}, _as("root", "admin"))
    assert (response.status_code, response.json()) == (
        404,
        {"detail": "unknown previous session: pam-alice-capture-9-9"},
    )


@pytest.mark.parametrize(("persist", "keep_days"), [(False, 0), (True, 0)])
def test_patching_with_previous_sessions_off_is_404(tmp_path: Path, persist: bool, keep_days: int) -> None:
    with TestClient(_app(tmp_path, keep_days=keep_days, persist=persist)) as off:
        response = _patch(off, PRIVATE, {"owner": "x"}, _as("root", "admin"))
    assert (response.status_code, response.json()) == (404, {"detail": f"unknown previous session: {PRIVATE}"})


def test_patching_unauthenticated_is_refused(client: TestClient) -> None:
    assert client.patch(f"/api/previous-sessions/{PRIVATE}", json={"owner": "x"}).status_code == 401


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({}, "a previous session takes owner and/or visibility, nothing else"),
        ({"owner": "x", "display_name": "y"}, "a previous session takes owner and/or visibility, nothing else"),
        ({"ended_at": 1.0}, "a previous session takes owner and/or visibility, nothing else"),
        ({"owner": None}, "owner must be a string"),
        ({"owner": 7}, "owner must be a string"),
        ({"owner": "x", "visibility": "secret"}, None),
        ({"visibility": None}, None),
    ],
)
def test_a_body_that_is_not_an_owner_or_visibility_is_422(
    client: TestClient, tmp_path: Path, body: dict[str, Any], detail: str | None
) -> None:
    before = _on_disk(tmp_path, PRIVATE)
    response = _patch(client, PRIVATE, body, _as("root", "admin"))
    assert response.status_code == 422
    if detail is not None:
        assert response.json() == {"detail": detail}
    else:
        # The live session's own refusal of the value.
        assert "invalid visibility" in response.json()["detail"]
    assert _on_disk(tmp_path, PRIVATE) == before


def _raw_patch(client: TestClient, session_id: str, content: bytes | None, headers: dict[str, str]) -> Any:
    return client.request("PATCH", f"/api/previous-sessions/{session_id}", content=content, headers=headers)


# Bodies no object can be read from: none at all, malformed JSON, bytes that
# are not UTF-8, and JSON that is not an object.
_UNREADABLE = [None, b"", b"{not json", b"\xff\xfe", b"[1]", b'"owner"', b"null", b"7"]


@pytest.mark.parametrize("content", _UNREADABLE)
def test_a_body_that_is_not_an_object_is_422(client: TestClient, tmp_path: Path, content: bytes | None) -> None:
    before = _on_disk(tmp_path, PRIVATE)
    response = _raw_patch(client, PRIVATE, content, _as("root", "admin"))
    assert (response.status_code, response.json()) == (422, {"detail": "invalid request body"})
    assert _on_disk(tmp_path, PRIVATE) == before


# 401, then 404, then 403, then 422, whatever the body: a caller learns
# nothing about the body's handling it could not learn without one.
@pytest.mark.parametrize("content", [*_UNREADABLE, b'{"colour": "red"}'])
def test_a_body_is_judged_only_after_the_caller_and_the_session(client: TestClient, content: bytes | None) -> None:
    assert _raw_patch(client, PRIVATE, content, {}).status_code == 401
    unknown = _raw_patch(client, "pam-alice-capture-9-9", content, _as("root", "admin"))
    assert unknown.status_code == 404
    refused = _raw_patch(client, PRIVATE, content, _as("alice"))
    assert (refused.status_code, refused.json()) == (403, {"detail": "admin privileges required"})


@pytest.mark.parametrize("damage", ["unlink", "garble"])
def test_a_file_gone_or_unreadable_since_listing_is_404_and_unlisted(
    client: TestClient, tmp_path: Path, damage: str
) -> None:
    path = tmp_path / "capture-sessions" / "previous" / f"{PRIVATE}.json"
    if damage == "unlink":
        path.unlink()
    else:
        path.write_bytes(b"{not json")
    response = _patch(client, PRIVATE, {"owner": "blackbetty:7"}, _as("root", "admin"))
    assert (response.status_code, response.json()) == (404, {"detail": f"unknown previous session: {PRIVATE}"})
    assert PRIVATE not in _listed(client, _as("root", "admin"))
    # Nothing is written in its place.
    assert path.exists() is (damage == "garble")


def test_a_write_that_fails_is_500_and_changes_nothing(client: TestClient, tmp_path: Path) -> None:
    previous = tmp_path / "capture-sessions" / "previous"
    before = _on_disk(tmp_path, PRIVATE)
    previous.chmod(0o500)
    try:
        response = _patch(client, PRIVATE, {"owner": "blackbetty:7"}, _as("root", "admin"))
    finally:
        previous.chmod(0o700)
    assert (response.status_code, response.json()) == (500, {"detail": "previous session not saved"})
    assert _on_disk(tmp_path, PRIVATE) == before
    assert _listed(client, _as("alice")) == [OPERATOR, PRIVATE]


def _crowded(tmp_path: Path, mine_ended_at: float) -> Iterator[TestClient]:
    """PRIVATE (alice's), ended at *mine_ended_at*, and twenty of blackbetty:7's, ended 100-119 s ago."""
    store = CaptureStateStore(tmp_path / "capture-sessions", persist=False, keep_days=7, scrollback_lines=100)
    now = time.time()

    async def seed() -> None:
        await _end(store, _definition(PRIVATE, "alice", "private"), "alice's screen", now + mine_ended_at)
        for i in range(20):
            await _end(store, _definition(f"pam-carl-capture-{i}-10", "blackbetty:7", "private"), "s", now - 100 - i)

    asyncio.run(seed())
    with TestClient(_app(tmp_path)) as test_client:
        yield test_client


def test_the_new_owners_cap_drops_its_oldest(tmp_path: Path) -> None:
    for client in _crowded(tmp_path, -5):
        response = _patch(client, PRIVATE, {"owner": "blackbetty:7"}, _as("root", "admin"))
        assert response.status_code == 200
        listed = _listed(client, _as("blackbetty:7"))
    assert listed == [PRIVATE, *(f"pam-carl-capture-{i}-10" for i in range(19))]
    assert not (tmp_path / "capture-sessions" / "previous" / "pam-carl-capture-19-10.json").exists()


def test_one_the_new_owners_cap_drops_is_404(tmp_path: Path) -> None:
    for client in _crowded(tmp_path, -1000):
        response = _patch(client, PRIVATE, {"owner": "blackbetty:7"}, _as("root", "admin"))
        assert (response.status_code, response.json()) == (404, {"detail": f"unknown previous session: {PRIVATE}"})
        assert len(_listed(client, _as("blackbetty:7"))) == 20
    assert not (tmp_path / "capture-sessions" / "previous" / f"{PRIVATE}.json").exists()


async def _store_with_one(tmp_path: Path) -> CaptureStateStore:
    store = CaptureStateStore(tmp_path / "capture-sessions", persist=False, keep_days=7, scrollback_lines=100)
    await _end(store, _definition(PRIVATE, "alice", "private"), "alice's screen", time.time())
    return store


async def test_the_store_answers_none_for_an_id_it_never_listed(tmp_path: Path) -> None:
    store = await _store_with_one(tmp_path)
    assert await store.update_previous("pam-alice-capture-9-9", {"owner": "x"}) is None
    assert [record.session_id for record in store.previous_sessions()] == [PRIVATE]


async def test_an_update_waits_for_the_sessions_lock(tmp_path: Path) -> None:
    # The same lock as every other write of the session's files, so two
    # updates (or an update and anything else) never interleave their writes.
    store = await _store_with_one(tmp_path)
    async with store._lock(PRIVATE):
        update = asyncio.create_task(store.update_previous(PRIVATE, {"owner": "blackbetty:7"}))
        await asyncio.sleep(0.05)
        assert not update.done()
    record = await update
    assert record is not None
    assert record.definition.owner == "blackbetty:7"
