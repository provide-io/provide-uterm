#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing suite for ``routes/pam_events.py``.

Measured 7.37% — 168 survivors. Same cause as the rest of ``routes/``: 9bc4dd0c
moved the handler out of its ``@router.post`` decorator into an undecorated
``pam_event_capability_handlers()`` factory, and mutmut skips decorated
functions, so every literal became mutable at once behind tests with 100% line
coverage.

This endpoint is the relay destination for PAM session events — a login on a
host turns into an observer session here — so the parts worth pinning are:

* **The session id is derived, not supplied.** ``pam-{username}-{slug}`` with the
  tty reduced by ``_tty_slug``. A mutation to the slug rules makes two different
  ttys collide onto one id, so one login's observer session silently adopts
  another's, and a ``close`` deletes the wrong one.
* **Role alternatives are not interchangeable.** ``authorize_pam_event_roles``
  resolves ``admin`` through the authorization service but any other role
  against the principal's own claim set. A mutation that swaps those branches
  either consults the service for a role it cannot answer, or accepts a
  self-asserted ``admin`` claim.
* **The open path is idempotent by design.** A ``ValueError`` from
  ``create_session`` is a conflict only when the session really is absent;
  Cloudflare KV's ``put`` overwrites, so a re-delivered open must not 409.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from provide.uterm.server.registry import SessionValidationError

MODULE = "provide.uterm.server.routes.pam_events"


def _handler() -> Any:
    from provide.uterm.server.routes.pam_events import pam_event_capability_handlers

    return pam_event_capability_handlers()["pam_events.ingest"]


def _principal(subject_id: str = "alice", roles: tuple[str, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(subject_id=subject_id, roles=set(roles))


def _authz(*, can_create: bool = True, admin: bool = False) -> MagicMock:
    az = MagicMock(name="authz")
    az.can_create_session = AsyncMock(return_value=can_create)
    az.is_admin = AsyncMock(return_value=admin)
    return az


def _registry(
    *,
    create_exc: BaseException | None = None,
    definition: Any = None,
) -> MagicMock:
    reg = MagicMock(name="registry")
    reg.create_session = AsyncMock(side_effect=create_exc)
    reg.delete_session = AsyncMock()
    reg.get_definition = AsyncMock(return_value=definition)
    return reg


def _request(
    *,
    body: Any = None,
    json_exc: BaseException | None = None,
    registry: Any = None,
    authz_obj: Any = None,
    principal: Any = None,
) -> MagicMock:
    req = MagicMock(name="request")
    req.app.state = SimpleNamespace(
        uterm_registry=registry if registry is not None else _registry(),
        uterm_authz=authz_obj if authz_obj is not None else _authz(),
    )
    req.state = SimpleNamespace(uterm_principal=principal if principal is not None else _principal())
    req.json = AsyncMock(side_effect=json_exc, return_value=body)
    return req


def _payload(reg: MagicMock) -> dict[str, Any]:
    return reg.create_session.await_args.args[0]


def _body_of(response: JSONResponse) -> bytes:
    return response.body


# ===========================================================================
# _tty_slug
# ===========================================================================


class TestTtySlug:
    """The tty half of the session id. Collisions here cross sessions."""

    @pytest.mark.parametrize(
        ("tty", "expected"),
        [
            ("/dev/pts/3", "3"),
            ("pts/3", "3"),
            ("tty1", "tty1"),
            ("ttyS0", "ttyS0"),
        ],
    )
    def test_takes_the_last_path_segment(self, tty: str, expected: str) -> None:
        from provide.uterm.server.routes.pam_events import _tty_slug

        assert _tty_slug(tty) == expected

    def test_runs_of_non_alphanumerics_collapse_to_one_dash(self) -> None:
        """``+`` in the pattern: without it "a__b" keeps two separators and two
        ttys that differ only in punctuation length map to different ids."""
        from provide.uterm.server.routes.pam_events import _tty_slug

        assert _tty_slug("a__b") == "a-b"
        assert _tty_slug("a.b_c") == "a-b-c"

    def test_leading_and_trailing_separators_are_trimmed(self) -> None:
        from provide.uterm.server.routes.pam_events import _tty_slug

        assert _tty_slug("--x--") == "x"
        assert _tty_slug("__x__") == "x"

    def test_only_separators_are_trimmed_not_arbitrary_characters(self) -> None:
        """``strip("-")`` takes a character SET, so a widened set eats real tty
        characters off both ends — and the id is derived from this, so two
        different ttys would collide onto one observer session."""
        from provide.uterm.server.routes.pam_events import _tty_slug

        assert _tty_slug("Xpts3X") == "Xpts3X"

    def test_an_empty_tty_falls_back_to_the_sentinel(self) -> None:
        from provide.uterm.server.routes.pam_events import _tty_slug

        assert _tty_slug("") == "tty"

    def test_an_all_punctuation_tty_falls_back_to_the_sentinel(self) -> None:
        """Strips to empty, and an empty slug would make the id end in a dash."""
        from provide.uterm.server.routes.pam_events import _tty_slug

        assert _tty_slug("///") == "tty"
        assert _tty_slug("___") == "tty"

    def test_alphanumerics_are_preserved_verbatim(self) -> None:
        from provide.uterm.server.routes.pam_events import _tty_slug

        assert _tty_slug("pts9Z") == "pts9Z"


# ===========================================================================
# authorize_pam_event_roles
# ===========================================================================


class TestAuthorizePamEventRoles:
    """``admin`` resolves through the service; every other role is a claim."""

    async def test_admin_is_resolved_through_the_authorization_service(self) -> None:
        from provide.uterm.server.routes.pam_events import authorize_pam_event_roles

        az = _authz(admin=True)
        principal = _principal(roles=())
        req = _request(authz_obj=az, principal=principal)

        assert await authorize_pam_event_roles(req, ("admin",)) is True
        az.is_admin.assert_awaited_once_with(principal)

    async def test_a_self_asserted_admin_claim_is_not_enough(self) -> None:
        """The claim set is attacker-influenced; the service is the authority."""
        from provide.uterm.server.routes.pam_events import authorize_pam_event_roles

        req = _request(authz_obj=_authz(admin=False), principal=_principal(roles=("admin",)))

        assert await authorize_pam_event_roles(req, ("admin",)) is False

    async def test_a_non_admin_role_is_matched_against_the_claim_set(self) -> None:
        from provide.uterm.server.routes.pam_events import authorize_pam_event_roles

        az = _authz(admin=False)
        req = _request(authz_obj=az, principal=_principal(roles=("operator",)))

        assert await authorize_pam_event_roles(req, ("operator",)) is True
        az.is_admin.assert_not_awaited()

    async def test_a_role_the_principal_lacks_is_refused(self) -> None:
        from provide.uterm.server.routes.pam_events import authorize_pam_event_roles

        req = _request(authz_obj=_authz(admin=False), principal=_principal(roles=("viewer",)))

        assert await authorize_pam_event_roles(req, ("operator",)) is False

    async def test_any_one_alternative_suffices(self) -> None:
        from provide.uterm.server.routes.pam_events import authorize_pam_event_roles

        req = _request(authz_obj=_authz(admin=False), principal=_principal(roles=("operator",)))

        assert await authorize_pam_event_roles(req, ("admin", "operator")) is True

    async def test_no_alternatives_means_refused(self) -> None:
        from provide.uterm.server.routes.pam_events import authorize_pam_event_roles

        req = _request(authz_obj=_authz(admin=True), principal=_principal(roles=("operator",)))

        assert await authorize_pam_event_roles(req, ()) is False


# ===========================================================================
# ingest — rejection paths
# ===========================================================================


class TestIngestRejections:
    async def test_a_principal_who_may_not_create_sessions_is_refused(self) -> None:
        reg = _registry()
        req = _request(body={"event": "open", "username": "u"}, registry=reg, authz_obj=_authz(can_create=False))

        with pytest.raises(HTTPException) as exc:
            await _handler()(req)

        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"
        reg.create_session.assert_not_awaited()
        reg.delete_session.assert_not_awaited()

    async def test_the_calling_principal_is_the_one_authorized(self) -> None:
        az = _authz()
        principal = _principal("bob")
        req = _request(body={"event": "open", "username": "u"}, authz_obj=az, principal=principal)

        await _handler()(req)

        az.can_create_session.assert_awaited_once_with(principal)

    async def test_an_unparseable_body_is_a_400(self) -> None:
        req = _request(json_exc=ValueError("not json"))

        response = await _handler()(req)

        assert isinstance(response, JSONResponse)
        assert response.status_code == 400
        assert _body_of(response) == b'{"error":"invalid_json"}'

    @pytest.mark.parametrize("body", [[], "text", 7, None])
    async def test_a_non_object_body_is_a_400(self, body: Any) -> None:
        """A JSON array parses fine but has no fields; treating it as a dict
        would raise later, inside the handler, as a 500."""
        req = _request(body=body)

        response = await _handler()(req)

        assert response.status_code == 400
        assert _body_of(response) == b'{"error":"invalid_json"}'

    async def test_an_unparseable_body_touches_no_session(self) -> None:
        reg = _registry()
        req = _request(json_exc=ValueError("nope"), registry=reg)

        await _handler()(req)

        reg.create_session.assert_not_awaited()
        reg.delete_session.assert_not_awaited()

    @pytest.mark.parametrize("event", ["", "opened", "OPEN", "delete", "close "])
    async def test_an_unrecognised_event_is_a_422_echoing_it(self, event: str) -> None:
        req = _request(body={"event": event, "username": "u"})

        response = await _handler()(req)

        assert response.status_code == 422
        assert b'"error":"unknown_event"' in _body_of(response)
        assert f'"event":"{event}"'.encode() in _body_of(response)

    async def test_a_missing_event_key_is_reported_as_the_empty_event(self) -> None:
        req = _request(body={"username": "u"})

        response = await _handler()(req)

        assert response.status_code == 422
        assert _body_of(response) == b'{"error":"unknown_event","event":""}'

    @pytest.mark.parametrize("username", ["", None])
    async def test_a_missing_username_is_a_422(self, username: Any) -> None:
        req = _request(body={"event": "open", "username": username})

        response = await _handler()(req)

        assert response.status_code == 422
        assert _body_of(response) == b'{"error":"missing_username"}'

    async def test_the_username_is_checked_after_the_event(self) -> None:
        """Order matters for the response a caller gets: an unknown event with
        no username reports the event, not the username."""
        req = _request(body={"event": "bogus"})

        response = await _handler()(req)

        assert b"unknown_event" in _body_of(response)


# ===========================================================================
# ingest — close
# ===========================================================================


class TestIngestClose:
    async def test_the_derived_session_is_deleted(self) -> None:
        reg = _registry()
        req = _request(body={"event": "close", "username": "alice", "tty": "/dev/pts/3"}, registry=reg)

        result = await _handler()(req)

        reg.delete_session.assert_awaited_once_with("pam-alice-3")
        assert result == {"ok": True, "session_id": "pam-alice-3", "action": "deleted"}

    async def test_closing_creates_nothing(self) -> None:
        reg = _registry()
        req = _request(body={"event": "close", "username": "alice"}, registry=reg)

        await _handler()(req)

        reg.create_session.assert_not_awaited()

    async def test_a_close_without_a_tty_uses_the_slug_sentinel(self) -> None:
        reg = _registry()
        req = _request(body={"event": "close", "username": "alice"}, registry=reg)

        result = await _handler()(req)

        assert result["session_id"] == "pam-alice-tty"


# ===========================================================================
# ingest — open
# ===========================================================================


class TestIngestOpen:
    async def test_the_observer_session_is_created_with_every_field(self) -> None:
        reg = _registry()
        req = _request(
            body={"event": "open", "username": "alice", "tty": "/dev/pts/3", "mode": "enforce"},
            registry=reg,
        )

        result = await _handler()(req)

        assert _payload(reg) == {
            "session_id": "pam-alice-3",
            "display_name": "alice (/dev/pts/3)",
            "connector_type": "shell",
            "connector_config": {},
            "input_mode": "open",
            "auto_start": False,
            "ephemeral": True,
            "tags": ["pam", "enforce", "alice"],
            "recording_enabled": False,
            "owner": "alice",
            "visibility": "operator",
        }
        assert result == {"ok": True, "session_id": "pam-alice-3", "action": "created"}

    async def test_a_missing_mode_is_recorded_as_notify(self) -> None:
        reg = _registry()
        req = _request(body={"event": "open", "username": "alice"}, registry=reg)

        await _handler()(req)

        assert _payload(reg)["tags"] == ["pam", "notify", "alice"]

    async def test_a_missing_tty_is_shown_as_pam_in_the_display_name(self) -> None:
        reg = _registry()
        req = _request(body={"event": "open", "username": "alice"}, registry=reg)

        await _handler()(req)

        assert _payload(reg)["display_name"] == "alice (pam)"

    async def test_the_session_is_visible_to_operators_and_owned_by_the_user(self) -> None:
        """Not the API principal: the observer belongs to whoever logged in."""
        reg = _registry()
        req = _request(
            body={"event": "open", "username": "alice"},
            registry=reg,
            principal=_principal("service-account"),
        )

        await _handler()(req)

        assert _payload(reg)["owner"] == "alice"
        assert _payload(reg)["visibility"] == "operator"

    async def test_a_pam_observer_never_records(self) -> None:
        reg = _registry()
        req = _request(body={"event": "open", "username": "alice"}, registry=reg)

        await _handler()(req)

        assert _payload(reg)["recording_enabled"] is False
        assert _payload(reg)["auto_start"] is False


class TestIngestOpenFailures:
    async def test_a_validation_error_is_a_422_carrying_the_message(self) -> None:
        reg = _registry(create_exc=SessionValidationError("bad session"))
        req = _request(body={"event": "open", "username": "alice"}, registry=reg)

        with pytest.raises(HTTPException) as exc:
            await _handler()(req)

        assert exc.value.status_code == 422
        assert exc.value.detail == "bad session"

    async def test_a_conflict_with_no_existing_session_is_a_409(self) -> None:
        reg = _registry(create_exc=ValueError("exists"), definition=None)
        req = _request(body={"event": "open", "username": "alice"}, registry=reg)

        with pytest.raises(HTTPException) as exc:
            await _handler()(req)

        assert exc.value.status_code == 409
        assert exc.value.detail == "exists"

    async def test_a_redelivered_open_succeeds_when_the_observer_already_exists(self) -> None:
        """Cloudflare KV's put overwrites, so the relay can deliver an open
        twice; the second must be a success, not a 409."""
        reg = _registry(create_exc=ValueError("exists"), definition=SimpleNamespace())
        req = _request(body={"event": "open", "username": "alice", "tty": "pts/1"}, registry=reg)

        result = await _handler()(req)

        assert result == {"ok": True, "session_id": "pam-alice-1", "action": "created"}

    async def test_the_conflict_check_asks_about_the_derived_session(self) -> None:
        reg = _registry(create_exc=ValueError("exists"), definition=SimpleNamespace())
        req = _request(body={"event": "open", "username": "alice", "tty": "pts/1"}, registry=reg)

        await _handler()(req)

        reg.get_definition.assert_awaited_once_with("pam-alice-1")


# ===========================================================================
# Module surface
# ===========================================================================


class TestModuleSurface:
    async def test_the_unregistered_placeholder_refuses_to_run(self) -> None:
        """Bound to every capability this module does not serve. Nothing routes
        to it, so without a test its mutants sit in mutmut's "no tests" state —
        not survivors, but still counted in the denominator. Exact equality, not
        pytest.raises(match=...): match is a regex SEARCH, so a padded message
        still matches and the mutant lives."""
        from provide.uterm.server.routes.pam_events import _unregistered_capability_handler

        with pytest.raises(RuntimeError) as exc:
            await _unregistered_capability_handler()
        assert str(exc.value) == "unregistered shared API capability invoked"

    def test_the_factory_serves_exactly_the_ingest_capability(self) -> None:
        from provide.uterm.server.routes.pam_events import pam_event_capability_handlers

        assert set(pam_event_capability_handlers()) == {"pam_events.ingest"}

    def test_registering_binds_the_shared_route_verbatim(self) -> None:
        from fastapi import APIRouter

        from provide.uterm.api_routes import API_ROUTES
        from provide.uterm.server.routes.pam_events import register_pam_event_routes

        router = APIRouter()
        register_pam_event_routes(router)

        expected = {
            (route.template, route.method.value) for route in API_ROUTES if route.capability == "pam_events.ingest"
        }
        bound = {(r.path, method) for r in router.routes for method in r.methods}  # type: ignore[attr-defined]
        assert bound == expected
        assert len(router.routes) == 1

    def test_registering_supplies_the_role_authorizer(self) -> None:
        """The shared RouteDef declares required roles, and bind_api_routes
        REFUSES to bind those without an authorizer. Dropping it would either
        raise at startup or, worse, bind the route unguarded."""
        from fastapi import APIRouter

        from provide.uterm.server.routes.pam_events import authorize_pam_event_roles, register_pam_event_routes

        bind = MagicMock()
        with patch(f"{MODULE}.bind_api_routes", bind):
            register_pam_event_routes(APIRouter())

        assert bind.call_args.kwargs == {"role_authorizer": authorize_pam_event_roles}
