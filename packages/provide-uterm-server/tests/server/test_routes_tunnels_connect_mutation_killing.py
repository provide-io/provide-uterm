#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing suite for ``routes/tunnels.py``: scrubbing + quick-connect.

``tunnels.py`` measured **4.98%** — 659 survivors. Same cause as the rest of
``routes/``: 9bc4dd0c moved these handlers out of ``@router.post`` decorators
into an undecorated ``tunnel_capability_handlers()`` factory, and mutmut skips
decorated functions, so every literal and branch in here became mutable at once
behind tests that execute the code (100% line coverage) without asserting on it.

This half covers ``_scrub_sensitive``, ``quick_connect``, the unregistered
placeholder and ``register_tunnel_routes``; the token handlers are in
``test_routes_tunnels_tokens_mutation_killing.py`` (777-line cap).

The security-load-bearing assertions here, worth keeping if this is ever
rewritten:

* ``_scrub_sensitive`` masks credentials **before** they reach the persisted
  session record or the audit trail, and the egress guard still sees the
  UNSCRUBBED config — a mutation that swaps those two arguments would either
  leak a password into storage or check a target of ``"***"``.
* The audit detail carries the connector type and nothing else. A mutation that
  widens it to the payload writes credentials to an append-only log.

Handlers are called straight off the factory dict — same function objects the
router binds, no path plumbing needed.
"""

from __future__ import annotations

import uuid as uuid_mod
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from provide.uterm.server.egress import EgressBlockedError
from provide.uterm.server.registry import SessionValidationError

MODULE = "provide.uterm.server.routes.tunnels"
_FIXED_UUID = uuid_mod.UUID("0123456789abcdef0123456789abcdef")
# uuid4().hex[:12] of the above — pins the slice bounds, not just the prefix.
_FIXED_SID = "connect-0123456789ab"


def _handler(name: str = "tunnels.connect") -> Any:
    from provide.uterm.server.routes.tunnels import tunnel_capability_handlers

    return tunnel_capability_handlers()[name]


def _config(
    *,
    block_private: bool = False,
    app_path: str = "/ui",
) -> SimpleNamespace:
    return SimpleNamespace(
        security=SimpleNamespace(block_private_connector_targets=block_private),
        ui=SimpleNamespace(app_path=app_path),
        server=SimpleNamespace(public_base_url=""),
        tunnel=SimpleNamespace(token_ttl_s=3600, ip_binding=False),
    )


def _request(
    *,
    registry: Any,
    authz_obj: Any,
    config: SimpleNamespace | None = None,
    principal: Any = None,
    client_host: str | None = "1.2.3.4",
) -> MagicMock:
    req = MagicMock(name="request")
    req.app.state = SimpleNamespace(
        uterm_registry=registry,
        uterm_authz=authz_obj,
        uterm_config=config if config is not None else _config(),
    )
    req.state = SimpleNamespace(uterm_principal=principal if principal is not None else _principal())
    req.client = SimpleNamespace(host=client_host) if client_host is not None else None
    return req


def _principal(subject_id: str = "alice") -> SimpleNamespace:
    return SimpleNamespace(subject_id=subject_id)


def _authz(*, can_create: bool = True) -> MagicMock:
    az = MagicMock(name="authz")
    az.can_create_session = AsyncMock(return_value=can_create)
    return az


def _registry(*, create_exc: BaseException | None = None) -> MagicMock:
    reg = MagicMock(name="registry")
    reg.create_session = AsyncMock(side_effect=create_exc, return_value=MagicMock(name="session"))
    return reg


class _Calls:
    """Captured collaborator calls for one quick_connect invocation."""

    def __init__(self) -> None:
        self.audit: MagicMock = MagicMock()
        self.egress: AsyncMock = AsyncMock()
        self.dump: MagicMock = MagicMock(return_value={"dumped": True})


async def _run_connect(
    payload: dict[str, Any],
    *,
    registry: Any = None,
    authz_obj: Any = None,
    config: SimpleNamespace | None = None,
    principal: Any = None,
    client_host: str | None = "1.2.3.4",
    egress_exc: BaseException | None = None,
) -> tuple[dict[str, Any], _Calls, MagicMock]:
    """Invoke quick_connect with every collaborator patched and captured."""
    calls = _Calls()
    if egress_exc is not None:
        calls.egress.side_effect = egress_exc
    reg = registry if registry is not None else _registry()
    req = _request(
        registry=reg,
        authz_obj=authz_obj if authz_obj is not None else _authz(),
        config=config,
        principal=principal,
        client_host=client_host,
    )
    with (
        patch(f"{MODULE}.uuid.uuid4", return_value=_FIXED_UUID),
        patch(f"{MODULE}.audit_event", calls.audit),
        patch(f"{MODULE}.assert_session_egress_allowed", calls.egress),
        patch(f"{MODULE}.model_dump", calls.dump),
    ):
        result = await _handler()(req, payload)
    return result, calls, reg


def _created_payload(reg: MagicMock) -> dict[str, Any]:
    return reg.create_session.await_args.args[0]


# ===========================================================================
# _scrub_sensitive
# ===========================================================================


class TestScrubSensitive:
    """Credential masking applied before persistence and before audit."""

    @pytest.mark.parametrize("key", ["password", "passphrase", "secret", "token"])
    def test_masks_every_sensitive_key(self, key: str) -> None:
        from provide.uterm.server.routes.tunnels import _scrub_sensitive

        assert _scrub_sensitive({key: "hunter2"}) == {key: "***"}

    def test_keeps_every_other_key_verbatim(self) -> None:
        """Over-masking would break connectors; the set is exact, not a guess."""
        from provide.uterm.server.routes.tunnels import _scrub_sensitive

        # Near-miss key names, not credentials: they prove the masked set is
        # exact rather than a substring match.
        config = {
            "host": "h",
            "port": 22,
            "username": "u",
            "tokens": "keep",
            "passwords": "keep",  # pragma: allowlist secret
        }  # pragma: allowlist secret
        assert _scrub_sensitive(config) == config

    def test_masks_only_the_sensitive_entries_of_a_mixed_config(self) -> None:
        from provide.uterm.server.routes.tunnels import _scrub_sensitive

        assert _scrub_sensitive({"host": "h", "password": "p", "token": "t"}) == {  # pragma: allowlist secret
            "host": "h",
            "password": "***",  # pragma: allowlist secret
            "token": "***",
        }

    def test_returns_a_copy_and_leaves_the_caller_dict_intact(self) -> None:
        """The connector still needs the plaintext from the original dict."""
        from provide.uterm.server.routes.tunnels import _scrub_sensitive

        original = {"password": "hunter2"}  # pragma: allowlist secret
        scrubbed = _scrub_sensitive(original)

        assert original == {"password": "hunter2"}  # pragma: allowlist secret
        assert scrubbed is not original

    def test_sentinel_is_exactly_three_asterisks(self) -> None:
        from provide.uterm.server.routes.tunnels import _SCRUB_SENTINEL, _SENSITIVE_CONFIG_KEYS

        assert _SCRUB_SENTINEL == "***"
        assert set(_SENSITIVE_CONFIG_KEYS) == {"password", "passphrase", "secret", "token"}

    def test_empty_config_stays_empty(self) -> None:
        from provide.uterm.server.routes.tunnels import _scrub_sensitive

        assert _scrub_sensitive({}) == {}


# ===========================================================================
# quick_connect — authorization
# ===========================================================================


class TestQuickConnectAuthorization:
    async def test_rejects_a_principal_who_may_not_create_sessions(self) -> None:
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz(can_create=False))

        with pytest.raises(HTTPException) as exc:
            await _handler()(req, {})

        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"
        reg.create_session.assert_not_awaited()

    async def test_authorizes_the_calling_principal(self) -> None:
        """Authorizing anyone else — or a null — is the whole bug class here."""
        az = _authz()
        principal = _principal("bob")
        await _run_connect({}, authz_obj=az, principal=principal)

        az.can_create_session.assert_awaited_once_with(principal)


# ===========================================================================
# quick_connect — payload parsing
# ===========================================================================


class TestQuickConnectPayloadDefaults:
    async def test_connector_type_defaults_to_ssh(self) -> None:
        _result, _calls, reg = await _run_connect({})
        assert _created_payload(reg)["connector_type"] == "ssh"

    async def test_connector_type_is_stripped(self) -> None:
        _result, _calls, reg = await _run_connect({"connector_type": "  telnet  "})
        assert _created_payload(reg)["connector_type"] == "telnet"

    async def test_display_name_defaults_to_the_connector_type(self) -> None:
        _result, _calls, reg = await _run_connect({"connector_type": "telnet"})
        assert _created_payload(reg)["display_name"] == "telnet"

    async def test_blank_display_name_falls_back_to_the_connector_type(self) -> None:
        """`or` then `or` again: a whitespace-only name strips to empty."""
        _result, _calls, reg = await _run_connect({"connector_type": "telnet", "display_name": "   "})
        assert _created_payload(reg)["display_name"] == "telnet"

    async def test_display_name_is_used_and_stripped_when_given(self) -> None:
        _result, _calls, reg = await _run_connect({"display_name": "  My Box  "})
        assert _created_payload(reg)["display_name"] == "My Box"

    async def test_input_mode_defaults_to_open(self) -> None:
        _result, _calls, reg = await _run_connect({})
        assert _created_payload(reg)["input_mode"] == "open"

    async def test_input_mode_is_taken_from_the_payload_and_stripped(self) -> None:
        _result, _calls, reg = await _run_connect({"input_mode": " hijack "})
        assert _created_payload(reg)["input_mode"] == "hijack"


class TestQuickConnectTags:
    async def test_missing_tags_become_an_empty_list(self) -> None:
        _result, _calls, reg = await _run_connect({})
        assert _created_payload(reg)["tags"] == []

    async def test_a_non_list_tags_value_is_discarded(self) -> None:
        """isinstance guard: a string would otherwise be split per character."""
        _result, _calls, reg = await _run_connect({"tags": "prod"})
        assert _created_payload(reg)["tags"] == []

    async def test_tags_are_stringified_stripped_and_emptied_out(self) -> None:
        _result, _calls, reg = await _run_connect({"tags": ["  prod  ", "", "   ", 7]})
        assert _created_payload(reg)["tags"] == ["prod", "7"]


class TestQuickConnectSessionId:
    async def test_session_id_is_the_connect_prefix_and_twelve_hex_characters(self) -> None:
        result, _calls, reg = await _run_connect({})

        assert _created_payload(reg)["session_id"] == _FIXED_SID
        assert result["session_id"] == _FIXED_SID


class TestQuickConnectConnectorConfig:
    @pytest.mark.parametrize(
        "field",
        [
            "connector_type",
            "display_name",
            "input_mode",
            "tags",
            "auto_start",
            "visibility",
            "owner",
            "recording_enabled",
            "ephemeral",
        ],
    )
    async def test_session_level_fields_never_reach_the_connector_config(self, field: str) -> None:
        """Connectors reject unknown keys, so a shrunken exclusion set is a 422."""
        _result, _calls, reg = await _run_connect({field: "x", "host": "h"})

        assert _created_payload(reg)["connector_config"] == {"host": "h"}

    async def test_remaining_payload_keys_are_passed_through(self) -> None:
        _result, _calls, reg = await _run_connect({"host": "h", "port": 22})
        assert _created_payload(reg)["connector_config"] == {"host": "h", "port": 22}

    async def test_the_persisted_config_is_scrubbed(self) -> None:
        _result, _calls, reg = await _run_connect({"host": "h", "password": "hunter2"})  # pragma: allowlist secret

        assert _created_payload(reg)["connector_config"] == {"host": "h", "password": "***"}  # pragma: allowlist secret

    async def test_the_egress_guard_sees_the_unscrubbed_config(self) -> None:
        """Order matters: the guard derives a host, and "***" is not a host.

        Scrubbing before the guard would silently disable egress checking for
        any connector whose target lives under a masked key.
        """
        payload = {"connector_type": "ssh", "host": "h", "password": "hunter2"}  # pragma: allowlist secret
        _result, calls, _reg = await _run_connect(payload)

        expected = {"host": "h", "password": "hunter2"}  # pragma: allowlist secret
        calls.egress.assert_awaited_once_with("ssh", expected, block_private=False)


class TestQuickConnectSessionPayloadConstants:
    async def test_the_session_is_created_ephemeral_private_autostarted_and_owned(self) -> None:
        _result, _calls, reg = await _run_connect({}, principal=_principal("carol"))
        created = _created_payload(reg)

        assert created["auto_start"] is True
        assert created["ephemeral"] is True
        assert created["visibility"] == "private"
        assert created["owner"] == "carol"

    async def test_recording_stays_unset_unless_requested(self) -> None:
        _result, _calls, reg = await _run_connect({})
        assert "recording_enabled" not in _created_payload(reg)

    async def test_recording_is_enabled_when_requested(self) -> None:
        _result, _calls, reg = await _run_connect({"recording_enabled": True})
        assert _created_payload(reg)["recording_enabled"] is True

    async def test_a_falsy_recording_flag_does_not_enable_recording(self) -> None:
        _result, _calls, reg = await _run_connect({"recording_enabled": False})
        assert "recording_enabled" not in _created_payload(reg)


# ===========================================================================
# quick_connect — egress guard and registry failures
# ===========================================================================


class TestQuickConnectEgressGuard:
    async def test_a_blocked_target_is_a_422_carrying_the_reason(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await _run_connect({"host": "169.254.169.254"}, egress_exc=EgressBlockedError("metadata endpoint"))

        assert exc.value.status_code == 422
        assert exc.value.detail == "metadata endpoint"

    async def test_a_blocked_target_never_creates_a_session(self) -> None:
        reg = _registry()
        with pytest.raises(HTTPException):
            await _run_connect({}, registry=reg, egress_exc=EgressBlockedError("nope"))

        reg.create_session.assert_not_awaited()

    async def test_the_configured_private_target_posture_is_forwarded(self) -> None:
        _result, calls, _reg = await _run_connect({}, config=_config(block_private=True))

        assert calls.egress.await_args.kwargs == {"block_private": True}


class TestQuickConnectRegistryFailures:
    async def test_a_validation_error_is_a_422_carrying_the_message(self) -> None:
        reg = _registry(create_exc=SessionValidationError("bad connector"))

        with pytest.raises(HTTPException) as exc:
            await _run_connect({}, registry=reg)

        assert exc.value.status_code == 422
        assert exc.value.detail == "bad connector"

    async def test_a_conflict_is_a_409_carrying_the_message(self) -> None:
        """Distinct status from the validation branch: a duplicate is retryable."""
        reg = _registry(create_exc=ValueError("already exists"))

        with pytest.raises(HTTPException) as exc:
            await _run_connect({}, registry=reg)

        assert exc.value.status_code == 409
        assert exc.value.detail == "already exists"


# ===========================================================================
# quick_connect — audit, tracing and response
# ===========================================================================


class TestQuickConnectAudit:
    async def test_the_audit_record_names_the_event_principal_session_and_source(self) -> None:
        _result, calls, _reg = await _run_connect({}, principal=_principal("dana"), client_host="10.0.0.9")

        calls.audit.assert_called_once_with(
            "session.create",
            principal="dana",
            session_id=_FIXED_SID,
            source_ip="10.0.0.9",
            detail={"connector_type": "ssh", "ephemeral": True},
        )

    async def test_the_audit_detail_carries_no_credential(self) -> None:
        """Audit sinks persist this verbatim; the payload must not reach them."""
        _result, calls, _reg = await _run_connect({"password": "hunter2", "host": "h"})  # pragma: allowlist secret

        assert calls.audit.call_args.kwargs["detail"] == {"connector_type": "ssh", "ephemeral": True}

    async def test_a_clientless_request_audits_an_unknown_source(self) -> None:
        _result, calls, _reg = await _run_connect({}, client_host=None)

        assert calls.audit.call_args.kwargs["source_ip"] == "unknown"


class TestQuickConnectTracing:
    async def test_the_span_is_named_and_attributed_for_the_operation(self) -> None:
        span = MagicMock(name="span")
        tracer = MagicMock(name="tracer")
        tracer.start_as_current_span.return_value.__enter__.return_value = span
        get_tracer = MagicMock(return_value=tracer)
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz(), principal=_principal("erin"))

        with (
            patch(f"{MODULE}.uuid.uuid4", return_value=_FIXED_UUID),
            patch(f"{MODULE}.audit_event"),
            patch(f"{MODULE}.assert_session_egress_allowed", AsyncMock()),
            patch(f"{MODULE}.model_dump", MagicMock(return_value={})),
            patch(f"{MODULE}.get_tracer", get_tracer),
        ):
            await _handler()(req, {})

        # The tracer is named for this module: a null name collapses every
        # route's spans into one anonymous tracer.
        get_tracer.assert_called_once_with(MODULE)
        tracer.start_as_current_span.assert_called_once_with("uterm.session.quick_connect")
        recorded = dict(call.args for call in span.set_attribute.call_args_list)
        assert recorded == {
            "uterm.session_id": _FIXED_SID,
            "uterm.operation": "session.quick_connect",
            "uterm.principal": "erin",
            "http.method": "POST",
            "http.target": "/api/connect",
        }


class TestQuickConnectResponse:
    async def test_the_url_points_at_the_configured_app_path(self) -> None:
        result, _calls, _reg = await _run_connect({}, config=_config(app_path="/console"))

        assert result["url"] == f"/console/session/{_FIXED_SID}"

    async def test_the_serialized_session_is_merged_into_the_response(self) -> None:
        result, calls, reg = await _run_connect({})

        calls.dump.assert_called_once_with(reg.create_session.return_value)
        assert result == {"session_id": _FIXED_SID, "url": f"/ui/session/{_FIXED_SID}", "dumped": True}

    async def test_the_serialized_session_may_override_neither_key_by_accident(self) -> None:
        """`**model_dump(session)` is last, so a session_id field in the dump
        wins. Pinned deliberately: swapping the spread ahead of the literals
        would silently change which id a client is told to connect to."""
        calls = _Calls()
        calls.dump.return_value = {"session_id": "from-dump"}
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz())

        with (
            patch(f"{MODULE}.uuid.uuid4", return_value=_FIXED_UUID),
            patch(f"{MODULE}.audit_event", calls.audit),
            patch(f"{MODULE}.assert_session_egress_allowed", calls.egress),
            patch(f"{MODULE}.model_dump", calls.dump),
        ):
            result = await _handler()(req, {})

        assert result["session_id"] == "from-dump"


# ===========================================================================
# Module surface
# ===========================================================================


class TestModuleSurface:
    async def test_the_unregistered_placeholder_refuses_to_run(self) -> None:
        """Bound to every capability this module does not serve. Nothing routes
        to it, so without a test its mutants are "no tests" — not survivors, but
        still counted in the denominator. Exact equality, not
        pytest.raises(match=...): match is a regex SEARCH, so a padded message
        still matches and the mutant lives."""
        from provide.uterm.server.routes.tunnels import _unregistered_capability_handler

        with pytest.raises(RuntimeError) as exc:
            await _unregistered_capability_handler()
        assert str(exc.value) == "unregistered shared API capability invoked"

    def test_the_factory_serves_exactly_the_four_tunnel_capabilities(self) -> None:
        from provide.uterm.server.routes.tunnels import tunnel_capability_handlers

        assert set(tunnel_capability_handlers()) == {
            "tunnels.connect",
            "tunnels.create",
            "tunnels.revoke_token",
            "tunnels.rotate_token",
        }

    def test_registering_binds_one_route_per_served_capability(self) -> None:
        """Selection is by capability membership: a mutated filter would bind
        the placeholder over every other family's routes, or none at all."""
        from fastapi import APIRouter

        from provide.uterm.api_routes import API_ROUTES
        from provide.uterm.server.routes.tunnels import register_tunnel_routes

        router = APIRouter()
        register_tunnel_routes(router)

        served = {"tunnels.connect", "tunnels.create", "tunnels.revoke_token", "tunnels.rotate_token"}
        # bind_api_routes passes route.template straight to add_api_route, so
        # the bound path is the shared template verbatim.
        expected = {(route.template, route.method.value) for route in API_ROUTES if route.capability in served}
        bound = {(r.path, method) for r in router.routes for method in r.methods}  # type: ignore[attr-defined]
        assert bound == expected

    def test_registering_binds_nothing_else(self) -> None:
        from fastapi import APIRouter

        from provide.uterm.server.routes.tunnels import register_tunnel_routes

        router = APIRouter()
        register_tunnel_routes(router)

        assert len(router.routes) == 4
