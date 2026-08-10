#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing suite for ``routes/sessions.py``: create / get / patch / delete / lifecycle.

Second of three files repairing ``sessions.py``'s 46.12% (see
``test_routes_sessions_list_mutation_killing.py`` for the cause). The
table-driven suite already kills the shared skeleton; these are the bodies.

What is load-bearing here:

* **Owner enforcement on create and patch.** A non-admin may only create a
  session owned by themselves, and the handler *overwrites* the owner rather
  than trusting the payload. Reassigning an existing session's owner is
  admin-only, and the ``allow_owner_change`` flag is passed through to the
  registry — the registry ignores an ``owner`` key unless it is set, so a
  mutation that hardcodes it either silently drops legitimate reassignments or
  lets any mutator take ownership.
* **Delete revokes tunnel tokens.** Otherwise an old share_token still
  authorizes a *replacement* session created later under the same id.
* **Disconnect and restart are deliberately gated on "connect"**, not on their
  own capability, so lifecycle control stays symmetric. Pinned so the
  asymmetry cannot be "fixed" by accident.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from provide.uterm.server.registry import SessionValidationError

MODULE = "provide.uterm.server.routes.sessions"
_SID = "s-1"


def _handler(name: str) -> Any:
    from provide.uterm.server.routes.sessions import session_capability_handlers

    return session_capability_handlers()[name]


def _principal(subject_id: str = "alice") -> SimpleNamespace:
    return SimpleNamespace(subject_id=subject_id, roles=set())


def _authz(
    *, admin: bool = False, can_create: bool = True, can_mutate: bool = True, can_read: bool = True
) -> MagicMock:
    az = MagicMock(name="authz")
    az.is_admin = AsyncMock(return_value=admin)
    az.can_create_session = AsyncMock(return_value=can_create)
    az.can_mutate_session = AsyncMock(return_value=can_mutate)
    az.can_read_session = AsyncMock(return_value=can_read)
    return az


def _registry(**methods: Any) -> MagicMock:
    reg = MagicMock(name="registry")
    reg.get_definition = AsyncMock(return_value=SimpleNamespace(session_id=_SID))
    for name in (
        "create_session",
        "get_session",
        "update_session",
        "delete_session",
        "start_session",
        "stop_session",
        "restart_session",
        "set_mode",
        "clear_session",
    ):
        setattr(reg, name, AsyncMock(return_value={"dumped": name}))
    for name, value in methods.items():
        setattr(reg, name, value)
    return reg


def _request(
    *,
    registry: Any,
    authz_obj: Any,
    principal: Any = None,
    tunnel_tokens: dict[str, Any] | None = None,
    client_host: str = "1.2.3.4",
) -> MagicMock:
    req = MagicMock(name="request")
    req.app.state = SimpleNamespace(
        uterm_registry=registry,
        uterm_authz=authz_obj,
        uterm_tunnel_tokens=tunnel_tokens if tunnel_tokens is not None else {},
    )
    req.state = SimpleNamespace(uterm_principal=principal if principal is not None else _principal())
    req.client = SimpleNamespace(host=client_host)
    return req


def _patched() -> Any:
    """Identity model_dump plus a silenced audit sink."""
    return (
        patch(f"{MODULE}.model_dump", side_effect=lambda session: session),
        patch(f"{MODULE}.audit_event", MagicMock()),
    )


async def _call(capability: str, req: Any, *args: Any) -> Any:
    dump, audit = _patched()
    with dump, audit:
        return await _handler(capability)(req, *args)


# ===========================================================================
# create_session
# ===========================================================================


class TestCreateSessionOwnership:
    async def test_a_non_admin_may_not_create_a_session_for_somebody_else(self) -> None:
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz(admin=False))

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.create", req, {"owner": "mallory"})

        assert exc.value.status_code == 403
        assert exc.value.detail == "owner must match authenticated subject"
        reg.create_session.assert_not_awaited()

    async def test_a_non_admin_naming_themselves_is_allowed(self) -> None:
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz(admin=False), principal=_principal("alice"))

        await _call("sessions.create", req, {"owner": "alice"})

        assert reg.create_session.await_args.args[0]["owner"] == "alice"

    async def test_a_non_admin_omitting_the_owner_gets_stamped_as_owner(self) -> None:
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz(admin=False), principal=_principal("alice"))

        await _call("sessions.create", req, {})

        assert reg.create_session.await_args.args[0]["owner"] == "alice"

    async def test_an_admin_may_create_a_session_owned_by_anybody(self) -> None:
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz(admin=True), principal=_principal("root"))

        await _call("sessions.create", req, {"owner": "mallory"})

        assert reg.create_session.await_args.args[0]["owner"] == "mallory"

    async def test_the_callers_payload_is_not_mutated(self) -> None:
        """``dict(payload)`` — the handler stamps a copy. Mutating the caller's
        dict would leak the override back into FastAPI's parsed body."""
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz(admin=False), principal=_principal("alice"))
        payload: dict[str, Any] = {}

        await _call("sessions.create", req, payload)

        assert payload == {}

    async def test_creation_privileges_are_checked_before_ownership(self) -> None:
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz(can_create=False, admin=False))

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.create", req, {"owner": "mallory"})

        assert exc.value.detail == "insufficient privileges"


class TestCreateSessionFailures:
    async def test_a_validation_error_is_a_422(self) -> None:
        reg = _registry(create_session=AsyncMock(side_effect=SessionValidationError("bad")))
        req = _request(registry=reg, authz_obj=_authz(admin=True))

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.create", req, {})

        assert exc.value.status_code == 422
        assert exc.value.detail == "bad"

    async def test_a_conflict_is_a_409(self) -> None:
        reg = _registry(create_session=AsyncMock(side_effect=ValueError("dupe")))
        req = _request(registry=reg, authz_obj=_authz(admin=True))

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.create", req, {})

        assert exc.value.status_code == 409
        assert exc.value.detail == "dupe"


class TestCreateSessionObservability:
    async def test_the_span_is_named_and_attributed(self) -> None:
        span = MagicMock(name="span")
        tracer = MagicMock(name="tracer")
        tracer.start_as_current_span.return_value.__enter__.return_value = span
        get_tracer = MagicMock(return_value=tracer)
        req = _request(registry=_registry(), authz_obj=_authz(admin=True), principal=_principal("root"))

        with (
            patch(f"{MODULE}.model_dump", side_effect=lambda s: s),
            patch(f"{MODULE}.audit_event"),
            patch(f"{MODULE}.get_tracer", get_tracer),
        ):
            await _handler("sessions.create")(req, {"session_id": "new-1"})

        get_tracer.assert_called_once_with(MODULE)
        tracer.start_as_current_span.assert_called_once_with("uterm.session.create")
        recorded = dict(call.args for call in span.set_attribute.call_args_list)
        assert recorded == {
            "uterm.session_id": "new-1",
            "uterm.operation": "session.create",
            "uterm.principal": "root",
            "http.method": "POST",
            "http.target": "/api/sessions",
        }

    async def test_the_audit_names_the_created_session(self) -> None:
        audit = MagicMock()
        req = _request(registry=_registry(), authz_obj=_authz(admin=True), principal=_principal("root"))

        with patch(f"{MODULE}.model_dump", side_effect=lambda s: s), patch(f"{MODULE}.audit_event", audit):
            await _handler("sessions.create")(req, {"session_id": "new-1"})

        audit.assert_called_once_with(
            "session.create",
            principal="root",
            session_id="new-1",
            source_ip="1.2.3.4",
        )


# ===========================================================================
# get_session
# ===========================================================================


class TestGetSession:
    async def test_a_missing_runtime_is_a_404_naming_the_session(self) -> None:
        reg = _registry(get_session=AsyncMock(side_effect=KeyError(_SID)))
        req = _request(registry=reg, authz_obj=_authz())

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.get", req, _SID)

        assert exc.value.status_code == 404
        assert exc.value.detail == f"unknown session: {_SID}"

    async def test_the_serialized_session_is_returned(self) -> None:
        reg = _registry(get_session=AsyncMock(return_value={"id": _SID}))
        req = _request(registry=reg, authz_obj=_authz())

        assert await _call("sessions.get", req, _SID) == {"id": _SID}

    async def test_an_unreadable_session_is_a_403(self) -> None:
        req = _request(registry=_registry(), authz_obj=_authz(can_read=False))

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.get", req, _SID)

        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"


# ===========================================================================
# patch_session
# ===========================================================================


class TestPatchSessionOwnerChange:
    async def test_reassigning_the_owner_requires_admin(self) -> None:
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz(admin=False))

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.update", req, _SID, {"owner": "mallory"})

        assert exc.value.status_code == 403
        assert exc.value.detail == "admin privileges required to reassign owner"
        reg.update_session.assert_not_awaited()

    async def test_an_admin_may_reassign_and_the_flag_is_passed_through(self) -> None:
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz(admin=True))

        await _call("sessions.update", req, _SID, {"owner": "mallory"})

        assert reg.update_session.await_args.kwargs == {"allow_owner_change": True}

    async def test_an_update_without_an_owner_key_never_allows_the_change(self) -> None:
        """Key PRESENCE, not truthiness: the registry ignores an owner field
        unless the flag is set, so a hardcoded True would let any mutator
        reassign ownership through an ordinary patch."""
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz(admin=True))

        await _call("sessions.update", req, _SID, {"display_name": "x"})

        assert reg.update_session.await_args.kwargs == {"allow_owner_change": False}

    async def test_an_explicit_null_owner_still_counts_as_a_change(self) -> None:
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz(admin=True))

        await _call("sessions.update", req, _SID, {"owner": None})

        assert reg.update_session.await_args.kwargs == {"allow_owner_change": True}

    async def test_a_non_admin_may_still_patch_other_fields(self) -> None:
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz(admin=False))

        await _call("sessions.update", req, _SID, {"display_name": "x"})

        assert reg.update_session.await_args.args == (_SID, {"display_name": "x"})


class TestPatchSessionFailures:
    async def test_a_validation_error_is_a_422(self) -> None:
        reg = _registry(update_session=AsyncMock(side_effect=SessionValidationError("bad")))
        req = _request(registry=reg, authz_obj=_authz(admin=True))

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.update", req, _SID, {})

        assert exc.value.status_code == 422
        assert exc.value.detail == "bad"

    async def test_a_vanished_session_is_a_404(self) -> None:
        reg = _registry(update_session=AsyncMock(side_effect=KeyError(_SID)))
        req = _request(registry=reg, authz_obj=_authz(admin=True))

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.update", req, _SID, {})

        assert exc.value.status_code == 404
        assert exc.value.detail == f"unknown session: {_SID}"

    async def test_an_unauthorized_mutator_is_a_403(self) -> None:
        req = _request(registry=_registry(), authz_obj=_authz(can_mutate=False, admin=True))

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.update", req, _SID, {})

        assert exc.value.detail == "insufficient privileges"


# ===========================================================================
# delete_session
# ===========================================================================


class TestDeleteSession:
    async def test_the_session_is_deleted_and_acknowledged(self) -> None:
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz())

        result = await _call("sessions.delete", req, _SID)

        reg.delete_session.assert_awaited_once_with(_SID)
        assert result == {"ok": True}

    async def test_tunnel_tokens_for_the_session_are_revoked(self) -> None:
        """Otherwise an old share_token authorizes a REPLACEMENT session created
        later under the same id."""
        tokens = {_SID: {"share_token_hash": "x"}, "other": {"share_token_hash": "y"}}
        req = _request(registry=_registry(), authz_obj=_authz(), tunnel_tokens=tokens)

        await _call("sessions.delete", req, _SID)

        assert tokens == {"other": {"share_token_hash": "y"}}

    async def test_deleting_a_session_with_no_tunnel_tokens_is_fine(self) -> None:
        tokens: dict[str, Any] = {}
        req = _request(registry=_registry(), authz_obj=_authz(), tunnel_tokens=tokens)

        assert await _call("sessions.delete", req, _SID) == {"ok": True}

    async def test_an_unauthorized_delete_touches_nothing(self) -> None:
        reg = _registry()
        tokens = {_SID: {"share_token_hash": "x"}}
        req = _request(registry=reg, authz_obj=_authz(can_mutate=False), tunnel_tokens=tokens)

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.delete", req, _SID)

        assert exc.value.status_code == 403
        reg.delete_session.assert_not_awaited()
        assert _SID in tokens

    async def test_the_span_carries_the_session_specific_path(self) -> None:
        span = MagicMock(name="span")
        tracer = MagicMock(name="tracer")
        tracer.start_as_current_span.return_value.__enter__.return_value = span
        req = _request(registry=_registry(), authz_obj=_authz(), principal=_principal("root"))

        with (
            patch(f"{MODULE}.audit_event"),
            patch(f"{MODULE}.get_tracer", MagicMock(return_value=tracer)),
        ):
            await _handler("sessions.delete")(req, _SID)

        tracer.start_as_current_span.assert_called_once_with("uterm.session.delete")
        recorded = dict(call.args for call in span.set_attribute.call_args_list)
        assert recorded == {
            "uterm.session_id": _SID,
            "uterm.operation": "session.delete",
            "uterm.principal": "root",
            "http.method": "DELETE",
            "http.target": f"/api/sessions/{_SID}",
        }

    async def test_the_audit_names_the_deleted_session(self) -> None:
        audit = MagicMock()
        req = _request(registry=_registry(), authz_obj=_authz(), principal=_principal("root"))

        with patch(f"{MODULE}.audit_event", audit):
            await _handler("sessions.delete")(req, _SID)

        audit.assert_called_once_with(
            "session.delete",
            principal="root",
            session_id=_SID,
            source_ip="1.2.3.4",
        )


# ===========================================================================
# Lifecycle: connect / disconnect / restart / clear
# ===========================================================================


class TestLifecycleHandlers:
    @pytest.mark.parametrize(
        ("capability", "method"),
        [
            ("sessions.connect", "start_session"),
            ("sessions.disconnect", "stop_session"),
            ("sessions.restart", "restart_session"),
            ("sessions.clear", "clear_session"),
        ],
    )
    async def test_each_drives_its_own_registry_method(self, capability: str, method: str) -> None:
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz())

        result = await _call(capability, req, _SID)

        getattr(reg, method).assert_awaited_once_with(_SID)
        assert result == {"dumped": method}

    @pytest.mark.parametrize(
        "capability",
        ["sessions.connect", "sessions.disconnect", "sessions.restart", "sessions.clear"],
    )
    async def test_a_vanished_session_is_a_404(self, capability: str) -> None:
        reg = _registry()
        for name in ("start_session", "stop_session", "restart_session", "clear_session"):
            setattr(reg, name, AsyncMock(side_effect=KeyError(_SID)))
        req = _request(registry=reg, authz_obj=_authz())

        with pytest.raises(HTTPException) as exc:
            await _call(capability, req, _SID)

        assert exc.value.status_code == 404
        assert exc.value.detail == f"unknown session: {_SID}"

    @pytest.mark.parametrize(
        ("capability", "action"),
        [
            ("sessions.connect", "session.control.connect"),
            ("sessions.disconnect", "session.control.connect"),
            ("sessions.restart", "session.control.connect"),
            ("sessions.clear", "session.control.clear"),
        ],
    )
    async def test_each_is_gated_on_its_documented_capability(self, capability: str, action: str) -> None:
        """Disconnect and restart share "connect" ON PURPOSE — whoever may start
        a session may stop it. Pinned so the asymmetry is not "corrected"."""
        az = _authz()
        req = _request(registry=_registry(), authz_obj=az)

        await _call(capability, req, _SID)

        assert az.can_mutate_session.await_args.args[2] == action


# ===========================================================================
# set_mode
# ===========================================================================


class TestSetMode:
    @pytest.mark.parametrize("mode", ["open", "hijack"])
    async def test_both_valid_modes_are_forwarded(self, mode: str) -> None:
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz())

        await _call("sessions.set_mode", req, _SID, {"input_mode": mode})

        reg.set_mode.assert_awaited_once_with(_SID, mode)

    async def test_the_mode_is_stripped_before_validation(self) -> None:
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz())

        await _call("sessions.set_mode", req, _SID, {"input_mode": "  open  "})

        reg.set_mode.assert_awaited_once_with(_SID, "open")

    @pytest.mark.parametrize("mode", ["", "OPEN", "readonly", "open hijack"])
    async def test_any_other_mode_is_a_422(self, mode: str) -> None:
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz())

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.set_mode", req, _SID, {"input_mode": mode})

        assert exc.value.status_code == 422
        assert exc.value.detail == "input_mode must be 'open' or 'hijack'"
        reg.set_mode.assert_not_awaited()

    async def test_a_missing_mode_key_is_a_422(self) -> None:
        req = _request(registry=_registry(), authz_obj=_authz())

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.set_mode", req, _SID, {})

        assert exc.value.status_code == 422

    async def test_mode_changes_are_gated_on_the_mode_capability(self) -> None:
        az = _authz()
        req = _request(registry=_registry(), authz_obj=az)

        await _call("sessions.set_mode", req, _SID, {"input_mode": "open"})

        assert az.can_mutate_session.await_args.args[2] == "session.control.mode"

    async def test_a_vanished_session_is_a_404(self) -> None:
        reg = _registry(set_mode=AsyncMock(side_effect=KeyError(_SID)))
        req = _request(registry=reg, authz_obj=_authz())

        with pytest.raises(HTTPException) as exc:
            await _call("sessions.set_mode", req, _SID, {"input_mode": "open"})

        assert exc.value.status_code == 404
        assert exc.value.detail == f"unknown session: {_SID}"
