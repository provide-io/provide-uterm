#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing suite for ``routes/tunnels.py``: ``create_tunnel``.

Part of the ``tunnels.py`` 4.98% repair (see the header of
``test_routes_tunnels_connect_mutation_killing.py`` for why this whole module
went unmutated until 9bc4dd0c un-decorated it). Split three ways for the
777-line cap: scrubbing + quick-connect, this file, and the revoke/rotate pair.

``create_tunnel`` mints the bearer tokens that grant access to a shared
terminal, so most of what is pinned here is security-load-bearing:

* Only BLAKE2b digests are stored; the plaintext tokens exist in the response
  and nowhere else. A mutation that stores the raw token turns a memory
  disclosure into a session takeover.
* The three tokens are distinct and independently generated — a mutation that
  reuses one value would hand a viewer the control token.
* The TTL is clamped at both ends. Without the floor a caller can request a
  1-second tunnel (useless but harmless); without the ceiling they can request
  a decade-long one, which is neither.
* ``issued_ip`` is recorded only when IP binding is configured, and the
  ``share_page`` routes HTTP tunnels to the inspector rather than the terminal.
"""

from __future__ import annotations

import uuid as uuid_mod
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from provide.uterm.server.registry import SessionValidationError

MODULE = "provide.uterm.server.routes.tunnels"
_FIXED_UUID = uuid_mod.UUID("0123456789abcdef0123456789abcdef")
_FIXED_TID = "tunnel-0123456789ab"
_NOW = 1_000_000.0
_TTL_DEFAULT = 3600
_TOKENS = ["worker-tok", "share-tok", "control-tok"]


def _handler() -> Any:
    from provide.uterm.server.routes.tunnels import tunnel_capability_handlers

    return tunnel_capability_handlers()["tunnels.create"]


def _config(
    *,
    token_ttl_s: int = _TTL_DEFAULT,
    ip_binding: bool = False,
    public_base_url: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        security=SimpleNamespace(block_private_connector_targets=False),
        ui=SimpleNamespace(app_path="/ui"),
        server=SimpleNamespace(public_base_url=public_base_url),
        tunnel=SimpleNamespace(token_ttl_s=token_ttl_s, ip_binding=ip_binding),
    )


def _request(
    *,
    registry: Any,
    authz_obj: Any,
    config: SimpleNamespace | None = None,
    tokens: dict[str, Any] | None = None,
    invites: dict[str, Any] | None = None,
    base_url: str = "http://host/",
    client_host: str | None = "1.2.3.4",
    subject_id: str = "alice",
) -> MagicMock:
    req = MagicMock(name="request")
    req.app.state = SimpleNamespace(
        uterm_registry=registry,
        uterm_authz=authz_obj,
        uterm_config=config if config is not None else _config(),
        uterm_tunnel_tokens=tokens if tokens is not None else {},
        uterm_tunnel_invites=invites if invites is not None else {},
    )
    req.state = SimpleNamespace(uterm_principal=SimpleNamespace(subject_id=subject_id))
    req.base_url = base_url
    req.client = SimpleNamespace(host=client_host) if client_host is not None else None
    return req


def _authz(*, can_create: bool = True) -> MagicMock:
    az = MagicMock(name="authz")
    az.can_create_session = AsyncMock(return_value=can_create)
    return az


def _registry(*, create_exc: BaseException | None = None) -> MagicMock:
    reg = MagicMock(name="registry")
    reg.create_session = AsyncMock(side_effect=create_exc)
    return reg


class _Env:
    """Everything one create_tunnel call touched."""

    def __init__(self) -> None:
        self.tokens: dict[str, Any] = {}
        self.invites: dict[str, Any] = {}
        self.audit = MagicMock()
        self.issue = MagicMock(return_value=("SHARE-INV", "CTRL-INV"))
        self.registry = _registry()
        self.result: dict[str, Any] = {}

    @property
    def stored(self) -> dict[str, Any]:
        return self.tokens[_FIXED_TID]

    @property
    def created(self) -> dict[str, Any]:
        return self.registry.create_session.await_args.args[0]


async def _run_create(
    payload: dict[str, Any],
    *,
    config: SimpleNamespace | None = None,
    registry: Any = None,
    authz_obj: Any = None,
    base_url: str = "http://host/",
    client_host: str | None = "1.2.3.4",
    subject_id: str = "alice",
) -> _Env:
    env = _Env()
    if registry is not None:
        env.registry = registry
    req = _request(
        registry=env.registry,
        authz_obj=authz_obj if authz_obj is not None else _authz(),
        config=config,
        tokens=env.tokens,
        invites=env.invites,
        base_url=base_url,
        client_host=client_host,
        subject_id=subject_id,
    )
    with (
        patch(f"{MODULE}.uuid.uuid4", return_value=_FIXED_UUID),
        patch(f"{MODULE}.time.time", return_value=_NOW),
        patch("secrets.token_urlsafe", side_effect=list(_TOKENS)),
        patch(f"{MODULE}.hash_token", side_effect=lambda tok: f"hash:{tok}"),
        patch(f"{MODULE}.issue_tunnel_invites", env.issue),
        patch(f"{MODULE}.audit_event", env.audit),
    ):
        env.result = await _handler()(req, payload)
    return env


# ===========================================================================
# Authorization and identity
# ===========================================================================


class TestCreateTunnelAuthorization:
    async def test_rejects_a_principal_who_may_not_create_sessions(self) -> None:
        reg = _registry()
        req = _request(registry=reg, authz_obj=_authz(can_create=False))

        with pytest.raises(HTTPException) as exc:
            await _handler()(req, {})

        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"
        reg.create_session.assert_not_awaited()

    async def test_the_calling_principal_is_the_one_authorized(self) -> None:
        az = _authz()
        env = _Env()
        req = _request(registry=env.registry, authz_obj=az, tokens=env.tokens, invites=env.invites)
        principal = req.state.uterm_principal

        with (
            patch(f"{MODULE}.uuid.uuid4", return_value=_FIXED_UUID),
            patch(f"{MODULE}.time.time", return_value=_NOW),
            patch("secrets.token_urlsafe", side_effect=list(_TOKENS)),
            patch(f"{MODULE}.hash_token", side_effect=lambda tok: f"hash:{tok}"),
            patch(f"{MODULE}.issue_tunnel_invites", env.issue),
            patch(f"{MODULE}.audit_event", env.audit),
        ):
            await _handler()(req, {})

        az.can_create_session.assert_awaited_once_with(principal)

    async def test_the_tunnel_id_is_the_prefix_and_twelve_hex_characters(self) -> None:
        env = await _run_create({})

        assert env.created["session_id"] == _FIXED_TID
        assert env.result["tunnel_id"] == _FIXED_TID


class TestCreateTunnelObservability:
    """The span and the log line are the only record that a tunnel was opened."""

    async def test_the_span_is_named_and_attributed_for_the_operation(self) -> None:
        span = MagicMock(name="span")
        tracer = MagicMock(name="tracer")
        tracer.start_as_current_span.return_value.__enter__.return_value = span
        get_tracer = MagicMock(return_value=tracer)
        env = _Env()
        req = _request(
            registry=env.registry,
            authz_obj=_authz(),
            tokens=env.tokens,
            invites=env.invites,
            subject_id="erin",
        )

        with (
            patch(f"{MODULE}.uuid.uuid4", return_value=_FIXED_UUID),
            patch(f"{MODULE}.time.time", return_value=_NOW),
            patch("secrets.token_urlsafe", side_effect=list(_TOKENS)),
            patch(f"{MODULE}.hash_token", side_effect=lambda tok: f"hash:{tok}"),
            patch(f"{MODULE}.issue_tunnel_invites", env.issue),
            patch(f"{MODULE}.audit_event", env.audit),
            patch(f"{MODULE}.get_tracer", get_tracer),
        ):
            await _handler()(req, {})

        get_tracer.assert_called_once_with(MODULE)
        tracer.start_as_current_span.assert_called_once_with("uterm.tunnel.create")
        recorded = dict(call.args for call in span.set_attribute.call_args_list)
        assert recorded == {
            "uterm.session_id": _FIXED_TID,
            "uterm.operation": "tunnel.create",
            "uterm.principal": "erin",
            "http.method": "POST",
            "http.target": "/api/tunnels",
        }

    async def test_the_creation_is_logged_with_the_tunnel_ttl_and_source(self) -> None:
        logger = MagicMock(name="logger")
        get_logger = MagicMock(return_value=logger)
        env = _Env()
        req = _request(
            registry=env.registry,
            authz_obj=_authz(),
            tokens=env.tokens,
            invites=env.invites,
            client_host="10.0.0.9",
        )

        with (
            patch(f"{MODULE}.uuid.uuid4", return_value=_FIXED_UUID),
            patch(f"{MODULE}.time.time", return_value=_NOW),
            patch("secrets.token_urlsafe", side_effect=list(_TOKENS)),
            patch(f"{MODULE}.hash_token", side_effect=lambda tok: f"hash:{tok}"),
            patch(f"{MODULE}.issue_tunnel_invites", env.issue),
            patch(f"{MODULE}.audit_event", env.audit),
            patch("provide.telemetry.get_logger", get_logger),
        ):
            await _handler()(req, {"ttl_s": 900})

        get_logger.assert_called_once_with(MODULE)
        logger.info.assert_called_once_with(
            "tunnel_token_created session_id=%s ttl_s=%d source_ip=%s",
            _FIXED_TID,
            900,
            "10.0.0.9",
        )


class TestCreateTunnelNaming:
    async def test_tunnel_type_defaults_to_terminal(self) -> None:
        env = await _run_create({})
        assert env.result["tunnel_type"] == "terminal"

    async def test_tunnel_type_is_taken_from_the_payload_and_stripped(self) -> None:
        env = await _run_create({"tunnel_type": "  http  "})
        assert env.result["tunnel_type"] == "http"

    async def test_display_name_defaults_to_tunnel(self) -> None:
        env = await _run_create({})
        assert env.result["display_name"] == "tunnel"

    async def test_a_blank_display_name_falls_back_to_the_default(self) -> None:
        env = await _run_create({"display_name": ""})
        assert env.result["display_name"] == "tunnel"

    async def test_display_name_is_used_and_stripped_when_given(self) -> None:
        env = await _run_create({"display_name": "  Demo  "})
        assert env.result["display_name"] == "Demo"


# ===========================================================================
# TTL clamping
# ===========================================================================


class TestCreateTunnelTtl:
    async def test_the_server_default_applies_when_none_is_requested(self) -> None:
        env = await _run_create({})
        assert env.result["expires_at"] == _NOW + _TTL_DEFAULT

    async def test_a_request_inside_the_range_is_honoured_exactly(self) -> None:
        env = await _run_create({"ttl_s": 900})
        assert env.result["expires_at"] == _NOW + 900

    async def test_a_shorter_request_is_raised_to_the_sixty_second_floor(self) -> None:
        env = await _run_create({"ttl_s": 1})
        assert env.result["expires_at"] == _NOW + 60

    async def test_exactly_sixty_seconds_is_allowed(self) -> None:
        """Boundary: max(60, …) keeps 60 itself rather than rounding it up."""
        env = await _run_create({"ttl_s": 60})
        assert env.result["expires_at"] == _NOW + 60

    async def test_a_longer_request_is_capped_at_twenty_four_times_the_default(self) -> None:
        env = await _run_create({"ttl_s": 10**9})
        assert env.result["expires_at"] == _NOW + _TTL_DEFAULT * 24

    async def test_the_cap_follows_the_configured_default(self) -> None:
        env = await _run_create({"ttl_s": 10**9}, config=_config(token_ttl_s=100))
        assert env.result["expires_at"] == _NOW + 2400

    async def test_a_string_ttl_is_coerced_to_an_int(self) -> None:
        env = await _run_create({"ttl_s": "900"})
        assert env.result["expires_at"] == _NOW + 900

    async def test_the_stored_expiry_matches_the_reported_one(self) -> None:
        env = await _run_create({"ttl_s": 900})
        assert env.stored["expires_at"] == env.result["expires_at"]

    async def test_the_creation_time_is_recorded(self) -> None:
        env = await _run_create({})
        assert env.stored["created_at"] == _NOW


# ===========================================================================
# Token minting and storage
# ===========================================================================


class TestCreateTunnelTokens:
    async def test_three_independent_tokens_are_minted_at_full_length(self) -> None:
        with (
            patch(f"{MODULE}.uuid.uuid4", return_value=_FIXED_UUID),
            patch(f"{MODULE}.time.time", return_value=_NOW),
            patch("secrets.token_urlsafe", side_effect=list(_TOKENS)) as mint,
            patch(f"{MODULE}.hash_token", side_effect=lambda tok: f"hash:{tok}"),
            patch(f"{MODULE}.issue_tunnel_invites", MagicMock(return_value=("s", "c"))),
            patch(f"{MODULE}.audit_event"),
        ):
            await _handler()(_request(registry=_registry(), authz_obj=_authz()), {})

        assert mint.call_count == 3
        assert [call.args for call in mint.call_args_list] == [(32,), (32,), (32,)]

    async def test_only_digests_are_stored_never_the_bearer_values(self) -> None:
        env = await _run_create({})

        assert env.stored["worker_token_hash"] == "hash:worker-tok"
        assert env.stored["share_token_hash"] == "hash:share-tok"
        assert env.stored["control_token_hash"] == "hash:control-tok"
        assert not {v for v in env.stored.values() if v in _TOKENS}

    async def test_the_worker_token_is_returned_in_plaintext_exactly_once(self) -> None:
        """The response is the only place the caller can ever read it."""
        env = await _run_create({})
        assert env.result["worker_token"] == "worker-tok"

    async def test_the_share_and_control_tokens_leave_only_as_invites(self) -> None:
        env = await _run_create({})

        assert "share-tok" not in str(env.result)
        assert "control-tok" not in str(env.result)


class TestCreateTunnelIpBinding:
    async def test_no_source_ip_is_recorded_when_binding_is_off(self) -> None:
        env = await _run_create({}, client_host="10.0.0.9")
        assert env.stored["issued_ip"] is None

    async def test_the_source_ip_is_recorded_when_binding_is_on(self) -> None:
        env = await _run_create({}, config=_config(ip_binding=True), client_host="10.0.0.9")
        assert env.stored["issued_ip"] == "10.0.0.9"

    async def test_the_bound_ip_is_handed_to_the_invite_issuer(self) -> None:
        env = await _run_create({}, config=_config(ip_binding=True), client_host="10.0.0.9")
        assert env.issue.call_args.kwargs["issued_ip"] == "10.0.0.9"

    async def test_the_invite_issuer_sees_no_ip_when_binding_is_off(self) -> None:
        env = await _run_create({}, client_host="10.0.0.9")
        assert env.issue.call_args.kwargs["issued_ip"] is None


class TestCreateTunnelSharePage:
    async def test_an_http_tunnel_lands_on_the_inspector(self) -> None:
        env = await _run_create({"tunnel_type": "http"})
        assert env.stored["share_page"] == "inspect"

    async def test_a_terminal_tunnel_lands_on_the_session_view(self) -> None:
        env = await _run_create({"tunnel_type": "terminal"})
        assert env.stored["share_page"] == "session"

    async def test_any_other_type_lands_on_the_session_view(self) -> None:
        env = await _run_create({"tunnel_type": "vnc"})
        assert env.stored["share_page"] == "session"

    async def test_the_stored_type_matches_the_requested_one(self) -> None:
        env = await _run_create({"tunnel_type": "http"})
        assert env.stored["tunnel_type"] == "http"


# ===========================================================================
# Session creation
# ===========================================================================


class TestCreateTunnelSession:
    async def test_the_backing_session_is_private_owned_and_not_auto_started(self) -> None:
        env = await _run_create({"tunnel_type": "http"}, subject_id="carol")

        assert env.created == {
            "session_id": _FIXED_TID,
            "display_name": "tunnel",
            "connector_type": "websocket",
            "connector_config": {"tunnel_type": "http"},
            "input_mode": "open",
            "auto_start": False,
            "ephemeral": True,
            "owner": "carol",
            "visibility": "private",
            "recording_enabled": True,
        }

    async def test_connector_target_validation_is_skipped_for_the_internal_socket(self) -> None:
        env = await _run_create({})
        assert env.registry.create_session.await_args.kwargs == {"validate_connector_target": False}

    async def test_a_validation_error_is_a_422_carrying_the_message(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await _run_create({}, registry=_registry(create_exc=SessionValidationError("bad")))

        assert exc.value.status_code == 422
        assert exc.value.detail == "bad"

    async def test_a_conflict_is_a_409_carrying_the_message(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await _run_create({}, registry=_registry(create_exc=ValueError("dupe")))

        assert exc.value.status_code == 409
        assert exc.value.detail == "dupe"

    async def test_a_failed_session_mints_no_stored_tokens(self) -> None:
        env = _Env()
        req = _request(
            registry=_registry(create_exc=ValueError("dupe")),
            authz_obj=_authz(),
            tokens=env.tokens,
            invites=env.invites,
        )
        with (
            patch(f"{MODULE}.uuid.uuid4", return_value=_FIXED_UUID),
            patch("secrets.token_urlsafe", side_effect=list(_TOKENS)),
            patch(f"{MODULE}.audit_event"),
            pytest.raises(HTTPException),
        ):
            await _handler()(req, {})

        assert env.tokens == {}


# ===========================================================================
# Invites, audit and the response
# ===========================================================================


class TestCreateTunnelInvites:
    async def test_the_issuer_receives_the_session_tokens_and_deadlines(self) -> None:
        env = await _run_create({"ttl_s": 900})

        assert env.issue.call_args.args == (env.invites,)
        assert env.issue.call_args.kwargs == {
            "session_id": _FIXED_TID,
            "share_token": "share-tok",
            "control_token": "control-tok",
            "tunnel_expires_at": _NOW + 900,
            "issued_ip": None,
            "now": _NOW,
        }


class TestCreateTunnelAudit:
    async def test_the_audit_record_names_the_event_principal_session_and_source(self) -> None:
        env = await _run_create({"tunnel_type": "http", "ttl_s": 900}, subject_id="dana", client_host="10.0.0.9")

        env.audit.assert_called_once_with(
            "tunnel.create",
            principal="dana",
            session_id=_FIXED_TID,
            source_ip="10.0.0.9",
            detail={"tunnel_type": "http", "ttl_s": 900},
        )

    async def test_the_audit_detail_records_the_clamped_ttl_not_the_requested_one(self) -> None:
        env = await _run_create({"ttl_s": 10**9})
        assert env.audit.call_args.kwargs["detail"]["ttl_s"] == _TTL_DEFAULT * 24

    async def test_no_token_reaches_the_audit_trail(self) -> None:
        env = await _run_create({})
        assert not {tok for tok in _TOKENS if tok in str(env.audit.call_args)}


class TestCreateTunnelResponse:
    async def test_the_endpoints_are_built_from_the_request_base_url(self) -> None:
        env = await _run_create({}, base_url="http://host/")

        assert env.result["ws_endpoint"] == f"ws://host/tunnel/{_FIXED_TID}"
        assert env.result["share_url"] == f"http://host/s/{_FIXED_TID}?invite=SHARE-INV"
        assert env.result["control_url"] == f"http://host/s/{_FIXED_TID}?invite=CTRL-INV"

    async def test_a_configured_public_base_url_wins_over_the_request(self) -> None:
        env = await _run_create({}, config=_config(public_base_url="https://public.example"))

        assert env.result["ws_endpoint"] == f"wss://public.example/tunnel/{_FIXED_TID}"
        assert env.result["share_url"].startswith("https://public.example/s/")

    async def test_a_trailing_slash_on_the_request_base_url_is_dropped(self) -> None:
        """Otherwise every URL in the response carries a doubled slash."""
        env = await _run_create({}, base_url="http://host/")
        assert "//tunnel" not in env.result["ws_endpoint"].removeprefix("ws://")

    async def test_only_the_trailing_slash_is_stripped_not_arbitrary_characters(self) -> None:
        """``rstrip("/")`` takes a character SET, so a mutated set silently eats
        real hostname characters. A host ending in one of them proves the set is
        exactly "/" — with base_url ``http://hostX/`` a mutation to ``"XX/XX"``
        strips the X too and points every URL at the wrong host."""
        env = await _run_create({}, base_url="http://hostX/")

        assert env.result["ws_endpoint"] == f"ws://hostX/tunnel/{_FIXED_TID}"

    async def test_an_https_base_becomes_a_wss_endpoint(self) -> None:
        env = await _run_create({}, base_url="https://host/")
        assert env.result["ws_endpoint"] == f"wss://host/tunnel/{_FIXED_TID}"

    async def test_the_response_carries_exactly_the_documented_fields(self) -> None:
        env = await _run_create({})

        assert set(env.result) == {
            "tunnel_id",
            "display_name",
            "tunnel_type",
            "ws_endpoint",
            "worker_token",
            "share_url",
            "control_url",
            "expires_at",
        }
