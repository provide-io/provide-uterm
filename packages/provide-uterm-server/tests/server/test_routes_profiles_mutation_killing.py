#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing suite for ``routes/profiles.py``.

Same cause as the rest of ``routes/``: 9bc4dd0c moved these handlers out of
``@router.*`` decorators into an undecorated factory, and mutmut skips decorated
functions.

Connection profiles are saved connection targets — host, port, username — so
what is pinned here is mostly about who may see and change them:

* **Listing is scoped by ownership** unless the caller is an admin. A mutation
  that inverts that hands every principal the full inventory of saved targets.
* **Create stamps the owner from the authenticated principal**, never from the
  payload, so a profile cannot be created pre-owned by somebody else.
* **Update applies an allowlist of eight fields.** Anything else in the payload
  is dropped, which is what stops ``owner``, ``profile_id`` or ``created_at``
  from being rewritten through an ordinary edit.
* **Connect requires BOTH grants** — read on the profile and create on
  sessions. Either alone is insufficient.
"""

from __future__ import annotations

import uuid as uuid_mod
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from provide.uterm.server.profiles import ConnectionProfile
from provide.uterm.server.registry import SessionValidationError

MODULE = "provide.uterm.server.routes.profiles"
_PID = "profile-1"
_FIXED_UUID = uuid_mod.UUID("0123456789abcdef0123456789abcdef")
_FIXED_NEW_PID = "profile-0123456789ab"
_FIXED_SID = "connect-0123456789ab"


def _handler(name: str) -> Any:
    from provide.uterm.server.routes.profiles import profile_capability_handlers

    return profile_capability_handlers()[name]


def _profile(**overrides: Any) -> ConnectionProfile:
    fields: dict[str, Any] = {
        "profile_id": _PID,
        "owner": "alice",
        "name": "Box",
        "connector_type": "ssh",
        "host": "h.example",
        "port": 22,
        "username": "u",
        "tags": ["prod"],
        "input_mode": "open",
        "recording_enabled": False,
        "visibility": "private",
        "created_at": 1.0,
        "updated_at": 2.0,
    }
    fields.update(overrides)
    return ConnectionProfile(**fields)


def _principal(subject_id: str = "alice") -> SimpleNamespace:
    return SimpleNamespace(subject_id=subject_id, roles=set())


def _authz(
    *,
    admin: bool = False,
    can_read_profile: bool = True,
    can_mutate_profile: bool = True,
    can_create: bool = True,
) -> MagicMock:
    az = MagicMock(name="authz")
    az.is_admin = AsyncMock(return_value=admin)
    az.can_read_profile = AsyncMock(return_value=can_read_profile)
    az.can_mutate_profile = AsyncMock(return_value=can_mutate_profile)
    az.can_create_session = AsyncMock(return_value=can_create)
    return az


def _store(*, profile: Any = None, listed: Any = None, update_exc: BaseException | None = None) -> MagicMock:
    store = MagicMock(name="store")
    store.list_profiles = AsyncMock(return_value=listed if listed is not None else [])
    store.get_profile = AsyncMock(return_value=profile)
    store.create_profile = AsyncMock(side_effect=lambda p: p)
    store.update_profile = AsyncMock(side_effect=update_exc, return_value=profile)
    store.delete_profile = AsyncMock()
    return store


def _request(
    *,
    store: Any = None,
    authz_obj: Any = None,
    registry: Any = None,
    principal: Any = None,
    app_path: str = "/ui",
) -> MagicMock:
    req = MagicMock(name="request")
    req.app.state = SimpleNamespace(
        uterm_profile_store=store if store is not None else _store(),
        uterm_authz=authz_obj if authz_obj is not None else _authz(),
        uterm_registry=registry if registry is not None else _registry(),
        uterm_config=SimpleNamespace(ui=SimpleNamespace(app_path=app_path)),
    )
    req.state = SimpleNamespace(uterm_principal=principal if principal is not None else _principal())
    return req


def _registry(*, create_exc: BaseException | None = None) -> MagicMock:
    reg = MagicMock(name="registry")
    reg.create_session = AsyncMock(side_effect=create_exc, return_value=MagicMock(name="session"))
    return reg


# ===========================================================================
# list_profiles
# ===========================================================================


class TestListProfiles:
    async def test_an_admin_sees_every_profile(self) -> None:
        store = _store(listed=[_profile()])
        req = _request(store=store, authz_obj=_authz(admin=True))

        await _handler("profiles.list")(req)

        store.list_profiles.assert_awaited_once_with()

    async def test_everybody_else_sees_only_their_own(self) -> None:
        """Saved targets name real hosts and usernames; the unscoped call would
        hand every principal the whole inventory."""
        store = _store(listed=[])
        req = _request(store=store, authz_obj=_authz(admin=False), principal=_principal("bob"))

        await _handler("profiles.list")(req)

        store.list_profiles.assert_awaited_once_with(owner="bob")

    async def test_the_profiles_are_serialized(self) -> None:
        store = _store(listed=[_profile()])
        req = _request(store=store, authz_obj=_authz(admin=True))

        result = await _handler("profiles.list")(req)

        assert result == [_profile().model_dump(mode="python")]


# ===========================================================================
# get_profile
# ===========================================================================


class TestGetProfile:
    async def test_a_missing_profile_is_a_404_naming_it(self) -> None:
        req = _request(store=_store(profile=None))

        with pytest.raises(HTTPException) as exc:
            await _handler("profiles.get")(req, _PID)

        assert exc.value.status_code == 404
        assert exc.value.detail == f"unknown profile: {_PID}"

    async def test_an_unreadable_profile_is_a_403(self) -> None:
        req = _request(store=_store(profile=_profile()), authz_obj=_authz(can_read_profile=False))

        with pytest.raises(HTTPException) as exc:
            await _handler("profiles.get")(req, _PID)

        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"

    async def test_readability_is_checked_against_the_fetched_profile(self) -> None:
        profile = _profile()
        az = _authz()
        principal = _principal("bob")
        req = _request(store=_store(profile=profile), authz_obj=az, principal=principal)

        await _handler("profiles.get")(req, _PID)

        az.can_read_profile.assert_awaited_once_with(principal, profile)

    async def test_the_named_profile_is_the_one_fetched(self) -> None:
        store = _store(profile=_profile())
        req = _request(store=store)

        await _handler("profiles.get")(req, _PID)

        store.get_profile.assert_awaited_once_with(_PID)


# ===========================================================================
# create_profile
# ===========================================================================


async def _create(payload: dict[str, Any], *, authz_obj: Any = None, principal: Any = None) -> ConnectionProfile:
    store = _store()
    req = _request(store=store, authz_obj=authz_obj, principal=principal)
    with patch(f"{MODULE}.uuid.uuid4", return_value=_FIXED_UUID), patch(f"{MODULE}.time.time", return_value=99.0):
        await _handler("profiles.create")(req, payload)
    return store.create_profile.await_args.args[0]


class TestCreateProfile:
    async def test_a_principal_who_may_not_create_sessions_is_refused(self) -> None:
        store = _store()
        req = _request(store=store, authz_obj=_authz(can_create=False))

        with pytest.raises(HTTPException) as exc:
            await _handler("profiles.create")(req, {})

        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"
        store.create_profile.assert_not_awaited()

    async def test_the_owner_is_the_authenticated_principal_not_the_payload(self) -> None:
        created = await _create({"owner": "mallory"}, principal=_principal("alice"))

        assert created.owner == "alice"

    async def test_the_id_is_the_prefix_and_twelve_hex_characters(self) -> None:
        created = await _create({})

        assert created.profile_id == _FIXED_NEW_PID

    async def test_both_timestamps_are_the_creation_time(self) -> None:
        created = await _create({})

        assert created.created_at == 99.0
        assert created.updated_at == 99.0

    async def test_an_unnamed_profile_gets_the_placeholder(self) -> None:
        created = await _create({})

        assert created.name == "Unnamed"

    async def test_a_blank_name_gets_the_placeholder(self) -> None:
        created = await _create({"name": ""})

        assert created.name == "Unnamed"

    async def test_the_name_is_stripped(self) -> None:
        created = await _create({"name": "  Box  "})

        assert created.name == "Box"

    @pytest.mark.parametrize(
        ("field", "default"),
        [("connector_type", "ssh"), ("input_mode", "open"), ("visibility", "private")],
    )
    async def test_every_enum_field_has_its_documented_default(self, field: str, default: str) -> None:
        created = await _create({})

        assert getattr(created, field) == default

    async def test_the_enum_fields_are_taken_from_the_payload(self) -> None:
        created = await _create({"connector_type": "telnet", "input_mode": "hijack", "visibility": "shared"})

        assert (created.connector_type, created.input_mode, created.visibility) == ("telnet", "hijack", "shared")

    async def test_recording_is_off_unless_requested(self) -> None:
        assert (await _create({})).recording_enabled is False
        assert (await _create({"recording_enabled": True})).recording_enabled is True

    @pytest.mark.parametrize("field", ["host", "username"])
    async def test_blank_text_fields_become_null(self, field: str) -> None:
        created = await _create({field: ""})

        assert getattr(created, field) is None

    async def test_text_fields_are_stripped(self) -> None:
        created = await _create({"host": "  h  ", "username": "  u  "})

        assert (created.host, created.username) == ("h", "u")

    async def test_the_port_is_coerced_to_an_int(self) -> None:
        created = await _create({"port": "2222"})

        assert created.port == 2222

    async def test_a_zero_port_is_treated_as_absent(self) -> None:
        created = await _create({"port": 0})

        assert created.port is None

    async def test_tags_are_stringified_stripped_and_emptied(self) -> None:
        created = await _create({"tags": ["  prod  ", "", "  ", 7]})

        assert created.tags == ["prod", "7"]

    async def test_a_non_list_tags_value_is_discarded(self) -> None:
        created = await _create({"tags": "prod"})

        assert created.tags == []

    async def test_the_stored_profile_is_returned(self) -> None:
        store = _store()
        req = _request(store=store)

        with patch(f"{MODULE}.uuid.uuid4", return_value=_FIXED_UUID), patch(f"{MODULE}.time.time", return_value=99.0):
            result = await _handler("profiles.create")(req, {})

        assert result["profile_id"] == _FIXED_NEW_PID


# ===========================================================================
# update_profile
# ===========================================================================


class TestUpdateProfile:
    async def test_a_missing_profile_is_a_404(self) -> None:
        req = _request(store=_store(profile=None))

        with pytest.raises(HTTPException) as exc:
            await _handler("profiles.update")(req, _PID, {})

        assert exc.value.status_code == 404
        assert exc.value.detail == f"unknown profile: {_PID}"

    async def test_an_unauthorized_mutator_is_a_403(self) -> None:
        store = _store(profile=_profile())
        req = _request(store=store, authz_obj=_authz(can_mutate_profile=False))

        with pytest.raises(HTTPException) as exc:
            await _handler("profiles.update")(req, _PID, {"name": "x"})

        assert exc.value.status_code == 403
        store.update_profile.assert_not_awaited()

    @pytest.mark.parametrize(
        "field",
        ["name", "host", "port", "username", "tags", "input_mode", "recording_enabled", "visibility"],
    )
    async def test_every_allowed_field_is_forwarded(self, field: str) -> None:
        store = _store(profile=_profile())
        req = _request(store=store)

        await _handler("profiles.update")(req, _PID, {field: "v"})

        assert store.update_profile.await_args.args[1] == {field: "v"}

    @pytest.mark.parametrize("field", ["owner", "profile_id", "created_at", "updated_at", "anything"])
    async def test_every_other_field_is_dropped(self, field: str) -> None:
        """The allowlist is what stops an ordinary edit from reassigning
        ownership or rewriting the profile's identity."""
        store = _store(profile=_profile())
        req = _request(store=store)

        await _handler("profiles.update")(req, _PID, {field: "v", "name": "keep"})

        assert store.update_profile.await_args.args[1] == {"name": "keep"}

    async def test_a_validation_error_is_a_422_carrying_the_message(self) -> None:
        try:
            ConnectionProfile(profile_id="p", owner="o", name="n", connector_type="nope")  # type: ignore[arg-type]
        except ValidationError as real_error:
            store = _store(profile=_profile(), update_exc=real_error)
        req = _request(store=store)

        with pytest.raises(HTTPException) as exc:
            await _handler("profiles.update")(req, _PID, {"name": "x"})

        assert exc.value.status_code == 422

    async def test_a_profile_that_vanishes_mid_update_is_a_404(self) -> None:
        store = _store(profile=_profile())
        store.update_profile = AsyncMock(return_value=None)
        req = _request(store=store)

        with pytest.raises(HTTPException) as exc:
            await _handler("profiles.update")(req, _PID, {"name": "x"})

        assert exc.value.status_code == 404
        assert exc.value.detail == f"unknown profile: {_PID}"

    async def test_the_updated_profile_is_returned(self) -> None:
        updated = _profile(name="New")
        store = _store(profile=_profile())
        store.update_profile = AsyncMock(return_value=updated)
        req = _request(store=store)

        result = await _handler("profiles.update")(req, _PID, {"name": "New"})

        assert result == updated.model_dump(mode="python")


# ===========================================================================
# delete_profile
# ===========================================================================


class TestDeleteProfile:
    async def test_a_missing_profile_is_a_404(self) -> None:
        req = _request(store=_store(profile=None))

        with pytest.raises(HTTPException) as exc:
            await _handler("profiles.delete")(req, _PID)

        assert exc.value.status_code == 404

    async def test_an_unauthorized_mutator_is_a_403_and_deletes_nothing(self) -> None:
        store = _store(profile=_profile())
        req = _request(store=store, authz_obj=_authz(can_mutate_profile=False))

        with pytest.raises(HTTPException) as exc:
            await _handler("profiles.delete")(req, _PID)

        assert exc.value.status_code == 403
        store.delete_profile.assert_not_awaited()

    async def test_the_named_profile_is_deleted_and_acknowledged(self) -> None:
        store = _store(profile=_profile())
        req = _request(store=store)

        result = await _handler("profiles.delete")(req, _PID)

        store.delete_profile.assert_awaited_once_with(_PID)
        assert result == {"ok": True}


# ===========================================================================
# connect_from_profile
# ===========================================================================


async def _connect(
    payload: dict[str, Any],
    *,
    profile: Any = None,
    authz_obj: Any = None,
    registry: Any = None,
    principal: Any = None,
) -> tuple[Any, MagicMock]:
    reg = registry if registry is not None else _registry()
    req = _request(
        store=_store(profile=profile if profile is not None else _profile()),
        authz_obj=authz_obj,
        registry=reg,
        principal=principal,
    )
    with (
        patch(f"{MODULE}.uuid.uuid4", return_value=_FIXED_UUID),
        patch(f"{MODULE}.model_dump", MagicMock(return_value={"dumped": True})),
    ):
        result = await _handler("profiles.connect")(req, _PID, payload)
    return result, reg


def _session_payload(reg: MagicMock) -> dict[str, Any]:
    return reg.create_session.await_args.args[0]


class TestConnectFromProfile:
    async def test_a_missing_profile_is_a_404(self) -> None:
        req = _request(store=_store(profile=None))

        with pytest.raises(HTTPException) as exc:
            await _handler("profiles.connect")(req, _PID, {})

        assert exc.value.status_code == 404

    async def test_an_unreadable_profile_is_a_403(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await _connect({}, authz_obj=_authz(can_read_profile=False))

        assert exc.value.status_code == 403

    async def test_read_alone_is_not_enough_to_start_a_session(self) -> None:
        """Both grants are required: seeing a saved target is not permission to
        connect to it."""
        with pytest.raises(HTTPException) as exc:
            await _connect({}, authz_obj=_authz(can_read_profile=True, can_create=False))

        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"

    async def test_the_connector_config_is_built_from_the_profile(self) -> None:
        _result, reg = await _connect({})

        assert _session_payload(reg)["connector_config"] == {"host": "h.example", "port": 22, "username": "u"}

    async def test_absent_profile_fields_are_omitted_entirely(self) -> None:
        _result, reg = await _connect({}, profile=_profile(host=None, port=None, username=None))

        assert _session_payload(reg)["connector_config"] == {}

    async def test_a_password_from_the_request_is_used(self) -> None:
        _result, reg = await _connect({"password": "hunter2"})  # pragma: allowlist secret

        assert _session_payload(reg)["connector_config"]["password"] == "hunter2"  # pragma: allowlist secret

    async def test_a_blank_password_is_not_forwarded(self) -> None:
        _result, reg = await _connect({"password": ""})

        assert "password" not in _session_payload(reg)["connector_config"]

    async def test_the_session_inherits_the_profile_and_is_owned_by_the_caller(self) -> None:
        _result, reg = await _connect({}, principal=_principal("bob"))
        payload = _session_payload(reg)

        assert payload["session_id"] == _FIXED_SID
        assert payload["display_name"] == "Box"
        assert payload["connector_type"] == "ssh"
        assert payload["input_mode"] == "open"
        assert payload["tags"] == ["prod"]
        assert payload["auto_start"] is True
        assert payload["ephemeral"] is True
        assert payload["visibility"] == "private"
        assert payload["owner"] == "bob"

    async def test_the_tag_list_is_copied_not_shared_with_the_profile(self) -> None:
        profile = _profile(tags=["prod"])
        _result, reg = await _connect({}, profile=profile)

        assert _session_payload(reg)["tags"] is not profile.tags

    async def test_recording_follows_the_profile(self) -> None:
        _result, off = await _connect({})
        assert "recording_enabled" not in _session_payload(off)

        _result, on = await _connect({}, profile=_profile(recording_enabled=True))
        assert _session_payload(on)["recording_enabled"] is True

    async def test_a_validation_error_is_a_422(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await _connect({}, registry=_registry(create_exc=SessionValidationError("bad")))

        assert exc.value.status_code == 422
        assert exc.value.detail == "bad"

    async def test_a_conflict_is_a_409(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await _connect({}, registry=_registry(create_exc=ValueError("dupe")))

        assert exc.value.status_code == 409
        assert exc.value.detail == "dupe"

    async def test_the_response_carries_the_session_id_url_and_dump(self) -> None:
        result, _reg = await _connect({})

        assert result == {"session_id": _FIXED_SID, "url": f"/ui/session/{_FIXED_SID}", "dumped": True}


# ===========================================================================
# Module surface
# ===========================================================================


class TestArgumentForwarding:
    """Mocks answer identically whatever they are handed, so a nulled argument
    changes no outcome and survives every behavioural assertion above. These
    pin the CALLS."""

    @pytest.mark.parametrize(
        ("capability", "extra"),
        [
            ("profiles.get", ()),
            ("profiles.update", ({},)),
            ("profiles.delete", ()),
            ("profiles.connect", ({},)),
        ],
    )
    async def test_the_named_profile_is_the_one_fetched(self, capability: str, extra: tuple[Any, ...]) -> None:
        store = _store(profile=_profile())
        req = _request(store=store)

        with (
            patch(f"{MODULE}.uuid.uuid4", return_value=_FIXED_UUID),
            patch(f"{MODULE}.model_dump", MagicMock(return_value={})),
        ):
            await _handler(capability)(req, _PID, *extra)

        store.get_profile.assert_awaited_once_with(_PID)

    async def test_connect_checks_readability_for_this_principal_and_profile(self) -> None:
        profile = _profile()
        az = _authz()
        principal = _principal("bob")
        req = _request(store=_store(profile=profile), authz_obj=az, principal=principal)

        with (
            patch(f"{MODULE}.uuid.uuid4", return_value=_FIXED_UUID),
            patch(f"{MODULE}.model_dump", MagicMock(return_value={})),
        ):
            await _handler("profiles.connect")(req, _PID, {})

        az.can_read_profile.assert_awaited_once_with(principal, profile)

    async def test_connect_checks_session_creation_for_the_calling_principal(self) -> None:
        az = _authz()
        principal = _principal("bob")
        req = _request(store=_store(profile=_profile()), authz_obj=az, principal=principal)

        with (
            patch(f"{MODULE}.uuid.uuid4", return_value=_FIXED_UUID),
            patch(f"{MODULE}.model_dump", MagicMock(return_value={})),
        ):
            await _handler("profiles.connect")(req, _PID, {})

        az.can_create_session.assert_awaited_once_with(principal)

    async def test_connect_serializes_the_session_the_registry_returned(self) -> None:
        reg = _registry()
        dump = MagicMock(return_value={})
        req = _request(store=_store(profile=_profile()), registry=reg)

        with patch(f"{MODULE}.uuid.uuid4", return_value=_FIXED_UUID), patch(f"{MODULE}.model_dump", dump):
            await _handler("profiles.connect")(req, _PID, {})

        dump.assert_called_once_with(reg.create_session.return_value)


class TestModuleSurface:
    async def test_the_unregistered_placeholder_refuses_to_run(self) -> None:
        from provide.uterm.server.routes.profiles import _unregistered_capability_handler

        with pytest.raises(RuntimeError) as exc:
            await _unregistered_capability_handler()
        assert str(exc.value) == "unregistered shared API capability invoked"

    def test_the_factory_serves_exactly_the_profile_capabilities(self) -> None:
        from provide.uterm.server.routes.profiles import profile_capability_handlers

        assert set(profile_capability_handlers()) == {
            "profiles.list",
            "profiles.create",
            "profiles.get",
            "profiles.update",
            "profiles.delete",
            "profiles.connect",
        }

    def test_registering_binds_every_served_capability(self) -> None:
        from fastapi import APIRouter

        from provide.uterm.api_routes import API_ROUTES
        from provide.uterm.server.routes.profiles import register_profile_routes

        router = APIRouter()
        register_profile_routes(router)

        served = {
            "profiles.list",
            "profiles.create",
            "profiles.get",
            "profiles.update",
            "profiles.delete",
            "profiles.connect",
        }
        expected = {(r.template, r.method.value) for r in API_ROUTES if r.capability in served}
        bound = {(r.path, m) for r in router.routes for m in r.methods}  # type: ignore[attr-defined]
        assert expected <= bound
