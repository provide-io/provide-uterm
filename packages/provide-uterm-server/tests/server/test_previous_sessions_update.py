#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""PATCH /api/previous-sessions/{id}: who owns an ended session, and who else may read it.

Only an admin may change who owns one, or its visibility: an admin as the
listing counts one (with the take-over capability) whom the live PATCH would
let change the session (``session.control.update``), never a share viewer,
since a login that ended before its owner was set is otherwise read by nobody
but an admin. The body is judged last: 401, 404 and 403 come before 422.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from provide.uterm.server.auth import Principal
from provide.uterm.server.authorization import LocalAuthorizationProvider
from provide.uterm.server.capture_state import CaptureStateStore
from provide.uterm.server.config_schema_session import SessionDefinition

from .previous_sessions_helpers import (
    BOBS,
    OPERATOR,
    PRIVATE,
    PUBLIC,
    PolicyRoles,
    as_caller,
    capture_definition,
    end_session,
    listed_ids,
    make_app,
    seeded_client,
)
from .viewer_revocation_helpers import bearer, mint_token


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    yield from seeded_client(tmp_path)


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
    listed = {
        e["id"]: e for e in client.get("/api/previous-sessions", headers=as_caller("root", "admin")).json()["sessions"]
    }
    response = _patch(client, PUBLIC, {"owner": "blackbetty:7", "visibility": "private"}, as_caller("root", "admin"))
    assert response.status_code == 200
    # The listing's entry, as GET of the list returns it: no screen.
    assert response.json() == {**listed[PUBLIC], "owner": "blackbetty:7"}
    on_disk = _on_disk(tmp_path, PUBLIC)
    assert (on_disk["session"]["owner"], on_disk["session"]["visibility"]) == ("blackbetty:7", "private")
    # Everything else is kept, the screen and when it ended above all.
    assert (on_disk["screen"]["ansi"], on_disk["scrollback"]) == ("bob's public screen", ["one", "two"])
    assert on_disk["ended_at"] == listed[PUBLIC]["ended_at"]
    # The new owner reads it; its old owner, and an operator now that it is private, do not.
    assert listed_ids(client, as_caller("blackbetty:7")) == [PUBLIC]
    read = client.get(f"/api/previous-sessions/{PUBLIC}", headers=as_caller("blackbetty:7"))
    assert (read.status_code, read.json()["screen"]) == (200, "bob's public screen")
    assert listed_ids(client, as_caller("bob")) == [BOBS]
    assert listed_ids(client, as_caller("carol", "operator")) == [OPERATOR]


def test_an_owner_set_survives_a_restart(client: TestClient, tmp_path: Path) -> None:
    assert _patch(client, PRIVATE, {"owner": "blackbetty:7"}, as_caller("root", "admin")).status_code == 200
    store = _reloaded(tmp_path)
    kept = store.previous_summary(PRIVATE)
    assert kept is not None
    assert (kept.definition.owner, kept.definition.visibility) == ("blackbetty:7", "private")
    record = asyncio.run(store.previous_session(PRIVATE))
    assert record is not None
    assert record.screen == "alice's private screen"


def test_one_field_leaves_the_other_as_it_was(client: TestClient, tmp_path: Path) -> None:
    assert _patch(client, OPERATOR, {"visibility": "public"}, as_caller("root", "admin")).status_code == 200
    assert _on_disk(tmp_path, OPERATOR)["session"]["owner"] == "alice"
    assert _on_disk(tmp_path, OPERATOR)["session"]["visibility"] == "public"
    assert listed_ids(client, as_caller("carol", "operator")) == [PUBLIC, OPERATOR]
    response = _patch(client, OPERATOR, {"owner": "blackbetty:7"}, as_caller("root", "admin"))
    assert response.json()["owner"] == "blackbetty:7"
    assert _on_disk(tmp_path, OPERATOR)["session"]["visibility"] == "public"


def test_a_change_is_audited(client: TestClient) -> None:
    audit = MagicMock()
    with patch("provide.uterm.server.routes.previous_sessions.audit_event", audit):
        _patch(client, PRIVATE, {"owner": "blackbetty:7", "visibility": "operator"}, as_caller("root", "admin"))
        # A refused one is not.
        _patch(client, PRIVATE, {"owner": "blackbetty:7"}, as_caller("alice"))
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
    response = _patch(client, PRIVATE, body, as_caller(*caller))
    assert (response.status_code, response.json()) == (403, {"detail": "admin privileges required"})
    assert _on_disk(tmp_path, PRIVATE) == before
    assert listed_ids(client, as_caller("alice")) == [OPERATOR, PRIVATE]


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


class _PolicyMutator(PolicyRoles):
    """PolicyRoles whose live-PATCH rule admits "policy-admin" alone, recording what it is asked."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str, str]] = []

    async def can_mutate_session(self, principal: Principal, session: SessionDefinition, action: str) -> bool:
        self.asked.append((principal.subject_id, session.session_id, action))
        return principal.subject_id == "policy-admin"


def test_the_admin_is_the_one_the_provider_names(client: TestClient) -> None:
    provider = _PolicyMutator()
    cast("Any", client.app).state.uterm_authz._provider = provider
    assert _patch(client, PRIVATE, {"owner": "x"}, as_caller("root", "admin")).status_code == 403
    assert _patch(client, PRIVATE, {"owner": "x"}, as_caller("policy-admin")).status_code == 200
    # The live PATCH's own question, about the definition the session ended with.
    assert ("policy-admin", PRIVATE, "session.control.update") in provider.asked


class _NoMutation(LocalAuthorizationProvider):
    """A provider whose live-PATCH rule refuses everyone, admins included."""

    async def can_mutate_session(self, principal: Principal, session: SessionDefinition, action: str) -> bool:
        return False


def test_an_admin_the_live_patch_would_refuse_is_refused(client: TestClient, tmp_path: Path) -> None:
    cast("Any", client.app).state.uterm_authz._provider = _NoMutation()
    before = _on_disk(tmp_path, BOBS)
    response = _patch(client, BOBS, {"owner": "eve"}, as_caller("root", "admin"))
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
    response = _patch(client, "pam-alice-capture-9-9", {"owner": "x"}, as_caller("root", "admin"))
    assert (response.status_code, response.json()) == (
        404,
        {"detail": "unknown previous session: pam-alice-capture-9-9"},
    )


@pytest.mark.parametrize(("persist", "keep_days"), [(False, 0), (True, 0)])
def test_patching_with_previous_sessions_off_is_404(tmp_path: Path, persist: bool, keep_days: int) -> None:
    with TestClient(make_app(tmp_path, keep_days=keep_days, persist=persist)) as off:
        response = _patch(off, PRIVATE, {"owner": "x"}, as_caller("root", "admin"))
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
    response = _patch(client, PRIVATE, body, as_caller("root", "admin"))
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
    response = _raw_patch(client, PRIVATE, content, as_caller("root", "admin"))
    assert (response.status_code, response.json()) == (422, {"detail": "invalid request body"})
    assert _on_disk(tmp_path, PRIVATE) == before


# 401, then 404, then 403, then 422, whatever the body: a caller learns
# nothing about the body's handling it could not learn without one.
@pytest.mark.parametrize("content", [*_UNREADABLE, b'{"colour": "red"}'])
def test_a_body_is_judged_only_after_the_caller_and_the_session(client: TestClient, content: bytes | None) -> None:
    assert _raw_patch(client, PRIVATE, content, {}).status_code == 401
    unknown = _raw_patch(client, "pam-alice-capture-9-9", content, as_caller("root", "admin"))
    assert unknown.status_code == 404
    refused = _raw_patch(client, PRIVATE, content, as_caller("alice"))
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
    response = _patch(client, PRIVATE, {"owner": "blackbetty:7"}, as_caller("root", "admin"))
    assert (response.status_code, response.json()) == (404, {"detail": f"unknown previous session: {PRIVATE}"})
    assert PRIVATE not in listed_ids(client, as_caller("root", "admin"))
    # Nothing is written in its place.
    assert path.exists() is (damage == "garble")


def test_a_write_that_fails_is_500_and_changes_nothing(client: TestClient, tmp_path: Path) -> None:
    previous = tmp_path / "capture-sessions" / "previous"
    before = _on_disk(tmp_path, PRIVATE)
    previous.chmod(0o500)
    try:
        response = _patch(client, PRIVATE, {"owner": "blackbetty:7"}, as_caller("root", "admin"))
    finally:
        previous.chmod(0o700)
    assert (response.status_code, response.json()) == (500, {"detail": "previous session not saved"})
    assert _on_disk(tmp_path, PRIVATE) == before
    assert listed_ids(client, as_caller("alice")) == [OPERATOR, PRIVATE]


def _crowded(tmp_path: Path, mine_ended_at: float) -> Iterator[TestClient]:
    """PRIVATE (alice's), ended at *mine_ended_at*, and twenty of blackbetty:7's, ended 100-119 s ago."""
    store = CaptureStateStore(tmp_path / "capture-sessions", persist=False, keep_days=7, scrollback_lines=100)
    now = time.time()

    async def seed() -> None:
        await end_session(store, capture_definition(PRIVATE, "alice", "private"), "alice's screen", now + mine_ended_at)
        for i in range(20):
            await end_session(
                store, capture_definition(f"pam-carl-capture-{i}-10", "blackbetty:7", "private"), "s", now - 100 - i
            )

    asyncio.run(seed())
    with TestClient(make_app(tmp_path)) as test_client:
        yield test_client


def test_the_new_owners_cap_drops_its_oldest(tmp_path: Path) -> None:
    for client in _crowded(tmp_path, -5):
        response = _patch(client, PRIVATE, {"owner": "blackbetty:7"}, as_caller("root", "admin"))
        assert response.status_code == 200
        listed = listed_ids(client, as_caller("blackbetty:7"))
    assert listed == [PRIVATE, *(f"pam-carl-capture-{i}-10" for i in range(19))]
    assert not (tmp_path / "capture-sessions" / "previous" / "pam-carl-capture-19-10.json").exists()


def test_one_the_new_owners_cap_drops_is_404(tmp_path: Path) -> None:
    for client in _crowded(tmp_path, -1000):
        response = _patch(client, PRIVATE, {"owner": "blackbetty:7"}, as_caller("root", "admin"))
        assert (response.status_code, response.json()) == (404, {"detail": f"unknown previous session: {PRIVATE}"})
        assert len(listed_ids(client, as_caller("blackbetty:7"))) == 20
    assert not (tmp_path / "capture-sessions" / "previous" / f"{PRIVATE}.json").exists()


async def _store_with_one(tmp_path: Path) -> CaptureStateStore:
    store = CaptureStateStore(tmp_path / "capture-sessions", persist=False, keep_days=7, scrollback_lines=100)
    await end_session(store, capture_definition(PRIVATE, "alice", "private"), "alice's screen", time.time())
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
