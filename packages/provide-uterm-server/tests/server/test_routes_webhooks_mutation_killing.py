#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing suite for ``routes/webhooks.py``.

Same cause as the rest of ``routes/``: 9bc4dd0c moved these handlers out of
``@router.*`` decorators into an undecorated factory, and mutmut skips decorated
functions.

A webhook makes the server issue outbound HTTP on session activity, so this
endpoint is an SSRF surface as much as a CRUD one:

* **The URL goes through ``manager.validate_url``** and a rejection is a 422.
  Skipping that call lets a caller point the server at loopback or the cloud
  metadata endpoint; the handler must not fall back to the raw payload value
  either, since validate_url returns a *normalised* URL.
* **The pattern goes through ``manager.validate_pattern``**, which is what
  bounds a caller-supplied regex.
* **Unregister verifies the webhook belongs to THIS session** before deleting
  it. Without that check a principal who may mutate their own session can
  delete another session's webhook by id.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

MODULE = "provide.uterm.server.routes.webhooks"
_SID = "s-1"
_WID = "wh-1"


def _handler(name: str) -> Any:
    from provide.uterm.server.routes.webhooks import webhook_capability_handlers

    return webhook_capability_handlers()[name]


def _principal(subject_id: str = "alice") -> SimpleNamespace:
    return SimpleNamespace(subject_id=subject_id, roles=set())


def _authz(*, can_mutate: bool = True) -> MagicMock:
    az = MagicMock(name="authz")
    az.can_mutate_session = AsyncMock(return_value=can_mutate)
    return az


def _registry(*, definition: Any = "present") -> MagicMock:
    reg = MagicMock(name="registry")
    resolved = SimpleNamespace(session_id=_SID) if definition == "present" else definition
    reg.get_definition = AsyncMock(return_value=resolved)
    return reg


def _cfg(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "webhook_id": _WID,
        "session_id": _SID,
        "url": "https://hook.example/x",
        "event_types": ["send"],
        "pattern": "boom",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _manager(
    *,
    validate_url_exc: BaseException | None = None,
    validate_pattern_exc: BaseException | None = None,
    registered: Any = None,
    listed: list[Any] | None = None,
    webhook: Any = "present",
) -> MagicMock:
    mgr = MagicMock(name="webhooks")
    mgr.validate_url = MagicMock(side_effect=validate_url_exc, return_value="https://normalised.example/x")
    mgr.validate_pattern = MagicMock(side_effect=validate_pattern_exc, return_value="compiled-pattern")
    mgr.register = AsyncMock(return_value=registered if registered is not None else _cfg())
    mgr.list_webhooks = MagicMock(return_value=listed if listed is not None else [])
    mgr.get_webhook = MagicMock(return_value=_cfg() if webhook == "present" else webhook)
    mgr.unregister = AsyncMock()
    return mgr


def _request(
    *,
    registry: Any = None,
    authz_obj: Any = None,
    manager: Any = "present",
    principal: Any = None,
    event_bus: Any = "bus",
) -> MagicMock:
    hub = SimpleNamespace(event_bus=event_bus) if event_bus is not None else SimpleNamespace()
    req = MagicMock(name="request")
    req.app.state = SimpleNamespace(
        uterm_registry=registry if registry is not None else _registry(),
        uterm_authz=authz_obj if authz_obj is not None else _authz(),
        uterm_webhooks=_manager() if manager == "present" else manager,
        uterm_hub=hub,
    )
    req.state = SimpleNamespace(uterm_principal=principal if principal is not None else _principal())
    return req


# ===========================================================================
# Shared preconditions across all three handlers
# ===========================================================================

_ALL = [
    ("sessions.webhooks.create", ({"url": "https://hook.example/x"},)),
    ("sessions.webhooks.list", ()),
    ("sessions.webhooks.delete", (_WID,)),
]


class TestSharedPreconditions:
    @pytest.mark.parametrize(("capability", "extra"), _ALL)
    async def test_an_unknown_session_is_a_404_naming_it(self, capability: str, extra: tuple[Any, ...]) -> None:
        req = _request(registry=_registry(definition=None))

        with pytest.raises(HTTPException) as exc:
            await _handler(capability)(req, _SID, *extra)

        assert exc.value.status_code == 404
        assert exc.value.detail == f"unknown session: {_SID}"

    @pytest.mark.parametrize(("capability", "extra"), _ALL)
    async def test_an_unauthorized_principal_is_a_403(self, capability: str, extra: tuple[Any, ...]) -> None:
        req = _request(authz_obj=_authz(can_mutate=False))

        with pytest.raises(HTTPException) as exc:
            await _handler(capability)(req, _SID, *extra)

        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"

    @pytest.mark.parametrize(("capability", "extra"), _ALL)
    async def test_the_named_session_is_the_one_resolved(self, capability: str, extra: tuple[Any, ...]) -> None:
        reg = _registry()
        req = _request(registry=reg)

        await _handler(capability)(req, _SID, *extra)

        reg.get_definition.assert_awaited_once_with(_SID)

    @pytest.mark.parametrize(("capability", "extra"), _ALL)
    async def test_the_update_capability_gates_every_handler(self, capability: str, extra: tuple[Any, ...]) -> None:
        az = _authz()
        principal = _principal("bob")
        definition = SimpleNamespace(session_id=_SID)
        req = _request(registry=_registry(definition=definition), authz_obj=az, principal=principal)

        await _handler(capability)(req, _SID, *extra)

        az.can_mutate_session.assert_awaited_once_with(principal, definition, "session.control.update")


# ===========================================================================
# register_webhook
# ===========================================================================


async def _register(payload: dict[str, Any], *, manager: Any = None, event_bus: Any = "bus") -> tuple[Any, MagicMock]:
    mgr = manager if manager is not None else _manager()
    req = _request(manager=mgr, event_bus=event_bus)
    result = await _handler("sessions.webhooks.create")(req, _SID, payload)
    return result, mgr


class TestRegisterWebhookUrl:
    @pytest.mark.parametrize("url", [None, "", 7, [], {"a": 1}])
    async def test_a_missing_or_non_string_url_is_a_422(self, url: Any) -> None:
        mgr = _manager()

        with pytest.raises(HTTPException) as exc:
            await _register({"url": url}, manager=mgr)

        assert exc.value.status_code == 422
        assert exc.value.detail == "url is required"
        mgr.register.assert_not_awaited()

    async def test_the_url_is_validated_before_registration(self) -> None:
        """The SSRF guard: without this the server can be aimed at loopback or
        a cloud metadata endpoint by anybody who may edit the session."""
        _result, mgr = await _register({"url": "http://169.254.169.254/"})

        mgr.validate_url.assert_called_once_with("http://169.254.169.254/")

    async def test_a_rejected_url_is_a_422_carrying_the_reason(self) -> None:
        mgr = _manager(validate_url_exc=ValueError("blocked host"))

        with pytest.raises(HTTPException) as exc:
            await _register({"url": "http://169.254.169.254/"}, manager=mgr)

        assert exc.value.status_code == 422
        assert exc.value.detail == "blocked host"
        mgr.register.assert_not_awaited()

    async def test_the_normalised_url_is_registered_not_the_raw_one(self) -> None:
        """validate_url returns a normalised value; registering the payload's
        original would re-open whatever normalisation closed."""
        _result, mgr = await _register({"url": "https://hook.example/x"})

        assert mgr.register.await_args.args[1] == "https://normalised.example/x"


class TestRegisterWebhookFilters:
    async def test_event_types_must_be_a_list(self) -> None:
        mgr = _manager()

        with pytest.raises(HTTPException) as exc:
            await _register({"url": "u", "event_types": "send"}, manager=mgr)

        assert exc.value.status_code == 422
        assert exc.value.detail == "event_types must be a list"
        mgr.register.assert_not_awaited()

    async def test_absent_event_types_are_allowed(self) -> None:
        _result, mgr = await _register({"url": "u"})

        assert mgr.register.await_args.kwargs["event_types"] is None

    async def test_an_event_type_list_is_forwarded(self) -> None:
        _result, mgr = await _register({"url": "u", "event_types": ["send", "read"]})

        assert mgr.register.await_args.kwargs["event_types"] == ["send", "read"]

    async def test_the_pattern_is_validated(self) -> None:
        _result, mgr = await _register({"url": "u", "pattern": "bo+m"})

        mgr.validate_pattern.assert_called_once_with("bo+m")

    async def test_a_rejected_pattern_is_a_422_carrying_the_reason(self) -> None:
        mgr = _manager(validate_pattern_exc=ValueError("pattern too long"))

        with pytest.raises(HTTPException) as exc:
            await _register({"url": "u", "pattern": "x" * 10_000}, manager=mgr)

        assert exc.value.status_code == 422
        assert exc.value.detail == "pattern too long"
        mgr.register.assert_not_awaited()

    async def test_the_validated_pattern_is_registered(self) -> None:
        _result, mgr = await _register({"url": "u", "pattern": "bo+m"})

        assert mgr.register.await_args.kwargs["pattern"] == "compiled-pattern"

    async def test_the_secret_is_forwarded_verbatim(self) -> None:
        _result, mgr = await _register({"url": "u", "secret": "s3cr3t"})  # pragma: allowlist secret

        assert mgr.register.await_args.kwargs["secret"] == "s3cr3t"  # pragma: allowlist secret

    async def test_no_secret_forwards_none(self) -> None:
        _result, mgr = await _register({"url": "u"})

        assert mgr.register.await_args.kwargs["secret"] is None


class TestRegisterWebhookWiring:
    async def test_the_session_and_event_bus_are_handed_to_the_manager(self) -> None:
        _result, mgr = await _register({"url": "u"})

        assert mgr.register.await_args.args[0] == _SID
        assert mgr.register.await_args.kwargs["event_bus"] == "bus"

    async def test_a_hub_without_an_event_bus_registers_none(self) -> None:
        _result, mgr = await _register({"url": "u"}, event_bus=None)

        assert mgr.register.await_args.kwargs["event_bus"] is None

    async def test_the_response_describes_the_registered_webhook(self) -> None:
        result, _mgr = await _register({"url": "u"})

        assert result == {
            "webhook_id": _WID,
            "session_id": _SID,
            "url": "https://hook.example/x",
            "event_types": ["send"],
            "pattern": "boom",
        }

    async def test_absent_event_types_serialize_as_null_not_an_empty_list(self) -> None:
        mgr = _manager(registered=_cfg(event_types=None))

        result, _mgr = await _register({"url": "u"}, manager=mgr)

        assert result["event_types"] is None

    async def test_event_types_are_copied_into_a_list(self) -> None:
        mgr = _manager(registered=_cfg(event_types=("send",)))

        result, _mgr = await _register({"url": "u"}, manager=mgr)

        assert result["event_types"] == ["send"]


# ===========================================================================
# list_webhooks
# ===========================================================================


class TestListWebhooks:
    async def test_the_session_is_the_one_listed(self) -> None:
        mgr = _manager()
        req = _request(manager=mgr)

        await _handler("sessions.webhooks.list")(req, _SID)

        mgr.list_webhooks.assert_called_once_with(_SID)

    async def test_no_webhooks_is_an_empty_list_not_an_error(self) -> None:
        req = _request(manager=_manager(listed=[]))

        assert await _handler("sessions.webhooks.list")(req, _SID) == {"webhooks": []}

    async def test_every_webhook_is_described(self) -> None:
        mgr = _manager(listed=[_cfg(), _cfg(webhook_id="wh-2", event_types=None)])
        req = _request(manager=mgr)

        result = await _handler("sessions.webhooks.list")(req, _SID)

        assert result == {
            "webhooks": [
                {
                    "webhook_id": _WID,
                    "session_id": _SID,
                    "url": "https://hook.example/x",
                    "event_types": ["send"],
                    "pattern": "boom",
                },
                {
                    "webhook_id": "wh-2",
                    "session_id": _SID,
                    "url": "https://hook.example/x",
                    "event_types": None,
                    "pattern": "boom",
                },
            ]
        }


# ===========================================================================
# unregister_webhook
# ===========================================================================


class TestUnregisterWebhook:
    async def test_an_unknown_webhook_is_a_404_naming_it(self) -> None:
        mgr = _manager(webhook=None)
        req = _request(manager=mgr)

        with pytest.raises(HTTPException) as exc:
            await _handler("sessions.webhooks.delete")(req, _SID, _WID)

        assert exc.value.status_code == 404
        assert exc.value.detail == f"unknown webhook: {_WID}"
        mgr.unregister.assert_not_awaited()

    async def test_a_webhook_belonging_to_another_session_is_a_404(self) -> None:
        """Ownership is per SESSION: without this a principal who may edit their
        own session could delete somebody else's webhook by guessing its id."""
        mgr = _manager(webhook=_cfg(session_id="someone-else"))
        req = _request(manager=mgr)

        with pytest.raises(HTTPException) as exc:
            await _handler("sessions.webhooks.delete")(req, _SID, _WID)

        assert exc.value.status_code == 404
        assert exc.value.detail == f"unknown webhook: {_WID}"
        mgr.unregister.assert_not_awaited()

    async def test_the_webhook_is_looked_up_by_id(self) -> None:
        mgr = _manager()
        req = _request(manager=mgr)

        await _handler("sessions.webhooks.delete")(req, _SID, _WID)

        mgr.get_webhook.assert_called_once_with(_WID)

    async def test_a_matching_webhook_is_unregistered_and_acknowledged(self) -> None:
        mgr = _manager()
        req = _request(manager=mgr)

        result = await _handler("sessions.webhooks.delete")(req, _SID, _WID)

        mgr.unregister.assert_awaited_once_with(_WID)
        assert result == {"ok": True, "webhook_id": _WID}


# ===========================================================================
# Module surface
# ===========================================================================


class TestModuleSurface:
    async def test_the_unregistered_placeholder_refuses_to_run(self) -> None:
        from provide.uterm.server.routes.webhooks import _unregistered_capability_handler

        with pytest.raises(RuntimeError) as exc:
            await _unregistered_capability_handler()
        assert str(exc.value) == "unregistered shared API capability invoked"

    def test_the_factory_serves_exactly_the_webhook_capabilities(self) -> None:
        from provide.uterm.server.routes.webhooks import webhook_capability_handlers

        assert set(webhook_capability_handlers()) == {
            "sessions.webhooks.create",
            "sessions.webhooks.list",
            "sessions.webhooks.delete",
        }

    def test_registering_binds_every_served_capability(self) -> None:
        from fastapi import APIRouter

        from provide.uterm.api_routes import API_ROUTES
        from provide.uterm.server.routes.webhooks import register_webhook_routes

        router = APIRouter()
        register_webhook_routes(router)

        served = {"sessions.webhooks.create", "sessions.webhooks.list", "sessions.webhooks.delete"}
        expected = {(r.template, r.method.value) for r in API_ROUTES if r.capability in served}
        bound = {(r.path, m) for r in router.routes for m in r.methods}  # type: ignore[attr-defined]
        assert expected <= bound
