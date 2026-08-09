#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing suite for ``routes/tunnels.py``: revoke and rotate.

Third of the three files repairing ``tunnels.py``'s 4.98% (see
``test_routes_tunnels_connect_mutation_killing.py`` for the cause). These two
handlers decide who may destroy or re-mint the bearer tokens of a shared
terminal, so the ownership checks are the point:

* **Revoke is deliberately asymmetric.** A missing session returns 200 without
  an ownership check — the tokens are already gone, so refusing would leak
  whether a tunnel id ever existed. A *present* session demands admin or owner.
  A mutation collapsing that into one rule either lets any principal revoke a
  live tunnel or turns cleanup into a 403 storm; both are pinned below.
* **Rotate is strict**: unknown session 404, wrong principal 403, session
  without tokens 404 — three distinct outcomes a mutation can merge.
* Rotation must invalidate the old invites before issuing new ones. Leaving
  them would keep handing out access to tokens that no longer exist.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

MODULE = "provide.uterm.server.routes.tunnels"
_TID = "tunnel-abc123"
_NOW = 2_000_000.0
_TTL = 3600
_TOKENS = ["worker-tok", "share-tok", "control-tok"]


def _handler(name: str) -> Any:
    from provide.uterm.server.routes.tunnels import tunnel_capability_handlers

    return tunnel_capability_handlers()[name]


def _config(*, token_ttl_s: int = _TTL, ip_binding: bool = False, public_base_url: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        security=SimpleNamespace(block_private_connector_targets=False),
        ui=SimpleNamespace(app_path="/ui"),
        server=SimpleNamespace(public_base_url=public_base_url),
        tunnel=SimpleNamespace(token_ttl_s=token_ttl_s, ip_binding=ip_binding),
    )


def _authz(*, admin: bool = False, owner: bool = False) -> MagicMock:
    az = MagicMock(name="authz")
    az.is_admin = AsyncMock(return_value=admin)
    az.is_owner = AsyncMock(return_value=owner)
    return az


def _registry(session: Any) -> MagicMock:
    reg = MagicMock(name="registry")
    reg.get_definition = AsyncMock(return_value=session)
    return reg


def _request(
    *,
    registry: Any,
    authz_obj: Any,
    tokens: dict[str, Any],
    invites: dict[str, Any],
    config: SimpleNamespace | None = None,
    base_url: str = "http://host/",
    client_host: str | None = "1.2.3.4",
    subject_id: str = "alice",
) -> MagicMock:
    req = MagicMock(name="request")
    req.app.state = SimpleNamespace(
        uterm_registry=registry,
        uterm_authz=authz_obj,
        uterm_config=config if config is not None else _config(),
        uterm_tunnel_tokens=tokens,
        uterm_tunnel_invites=invites,
    )
    req.state = SimpleNamespace(uterm_principal=SimpleNamespace(subject_id=subject_id))
    req.base_url = base_url
    req.client = SimpleNamespace(host=client_host) if client_host is not None else None
    return req


class _Env:
    def __init__(self) -> None:
        self.tokens: dict[str, Any] = {}
        self.invites: dict[str, Any] = {}
        self.audit = MagicMock()
        self.discard = MagicMock()
        self.issue = MagicMock(return_value=("SHARE-INV", "CTRL-INV"))
        self.logger = MagicMock()
        self.result: Any = None


def _patches(env: _Env) -> tuple[Any, ...]:
    return (
        patch(f"{MODULE}.time.time", return_value=_NOW),
        patch("secrets.token_urlsafe", side_effect=list(_TOKENS)),
        patch(f"{MODULE}.hash_token", side_effect=lambda tok: f"hash:{tok}"),
        patch(f"{MODULE}.discard_tunnel_invites_for_session", env.discard),
        patch(f"{MODULE}.issue_tunnel_invites", env.issue),
        patch(f"{MODULE}.audit_event", env.audit),
        patch("provide.telemetry.get_logger", MagicMock(return_value=env.logger)),
    )


async def _run(
    capability: str,
    *,
    session: Any,
    authz_obj: MagicMock,
    tokens: dict[str, Any] | None = None,
    config: SimpleNamespace | None = None,
    client_host: str | None = "1.2.3.4",
    subject_id: str = "alice",
    base_url: str = "http://host/",
) -> _Env:
    env = _Env()
    if tokens is not None:
        env.tokens.update(tokens)
    req = _request(
        registry=_registry(session),
        authz_obj=authz_obj,
        tokens=env.tokens,
        invites=env.invites,
        config=config,
        base_url=base_url,
        client_host=client_host,
        subject_id=subject_id,
    )
    patchers = _patches(env)
    for p in patchers:
        p.start()
    try:
        env.result = await _handler(capability)(req, _TID)
    finally:
        for p in patchers:
            p.stop()
    return env


async def _expect_http_error(capability: str, **kwargs: Any) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        await _run(capability, **kwargs)
    return exc.value


def _live_tokens() -> dict[str, Any]:
    return {_TID: {"tunnel_type": "http", "created_at": 1.0, "expires_at": 2.0}}


# ===========================================================================
# revoke_tunnel_tokens
# ===========================================================================


class TestRevokeAuthorization:
    async def test_a_stranger_may_not_revoke_a_live_tunnel(self) -> None:
        exc = await _expect_http_error(
            "tunnels.revoke_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=False, owner=False),
            tokens=_live_tokens(),
        )

        assert exc.status_code == 403
        assert exc.detail == "insufficient privileges"

    async def test_a_refused_revoke_leaves_the_tokens_in_place(self) -> None:
        env = _Env()
        env.tokens.update(_live_tokens())
        req = _request(
            registry=_registry(SimpleNamespace()),
            authz_obj=_authz(),
            tokens=env.tokens,
            invites=env.invites,
        )

        with pytest.raises(HTTPException):
            await _handler("tunnels.revoke_token")(req, _TID)

        assert _TID in env.tokens

    async def test_an_admin_may_revoke(self) -> None:
        env = await _run(
            "tunnels.revoke_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens=_live_tokens(),
        )
        assert env.result == {"ok": True, "session_id": _TID}

    async def test_the_owner_may_revoke(self) -> None:
        env = await _run(
            "tunnels.revoke_token",
            session=SimpleNamespace(),
            authz_obj=_authz(owner=True),
            tokens=_live_tokens(),
        )
        assert env.result == {"ok": True, "session_id": _TID}

    async def test_ownership_is_checked_for_this_principal_against_this_session(self) -> None:
        """Both arguments matter: a dropped or nulled one authorizes the wrong
        subject, or checks ownership of the wrong record."""
        session = SimpleNamespace(name="definition")
        az = _authz(admin=False, owner=True)
        env = _Env()
        env.tokens.update(_live_tokens())
        req = _request(registry=_registry(session), authz_obj=az, tokens=env.tokens, invites=env.invites)
        principal = req.state.uterm_principal

        patchers = _patches(env)
        for p in patchers:
            p.start()
        try:
            await _handler("tunnels.revoke_token")(req, _TID)
        finally:
            for p in patchers:
                p.stop()

        az.is_admin.assert_awaited_once_with(principal)
        az.is_owner.assert_awaited_once_with(principal, session)

    async def test_the_named_tunnel_is_the_one_looked_up(self) -> None:
        env = _Env()
        req = _request(registry=_registry(None), authz_obj=_authz(), tokens=env.tokens, invites=env.invites)

        patchers = _patches(env)
        for p in patchers:
            p.start()
        try:
            await _handler("tunnels.revoke_token")(req, _TID)
        finally:
            for p in patchers:
                p.stop()

        req.app.state.uterm_registry.get_definition.assert_awaited_once_with(_TID)

    async def test_a_missing_session_is_revoked_without_an_ownership_check(self) -> None:
        """Idempotent by design: nothing is left to protect, and refusing would
        disclose whether a tunnel id was ever real."""
        az = _authz(admin=False, owner=False)
        env = await _run("tunnels.revoke_token", session=None, authz_obj=az)

        assert env.result == {"ok": True, "session_id": _TID}
        az.is_admin.assert_not_awaited()
        az.is_owner.assert_not_awaited()


class TestRevokeEffects:
    async def test_the_tokens_are_dropped(self) -> None:
        env = await _run(
            "tunnels.revoke_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens=_live_tokens(),
        )
        assert _TID not in env.tokens

    async def test_the_pending_invites_are_discarded(self) -> None:
        env = await _run(
            "tunnels.revoke_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens=_live_tokens(),
        )
        env.discard.assert_called_once_with(env.invites, _TID)

    async def test_revoking_an_already_revoked_tunnel_still_succeeds(self) -> None:
        env = await _run("tunnels.revoke_token", session=None, authz_obj=_authz())
        assert env.result == {"ok": True, "session_id": _TID}

    async def test_the_log_records_whether_tokens_were_actually_found(self) -> None:
        env = await _run(
            "tunnels.revoke_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens=_live_tokens(),
        )
        env.logger.info.assert_called_once_with("tunnel_token_revoked session_id=%s found=%s", _TID, True)

    async def test_the_logger_is_named_for_this_module(self) -> None:
        """A null name collapses every route's records into one anonymous logger."""
        logger = MagicMock(name="logger")
        get_logger = MagicMock(return_value=logger)
        env = _Env()
        req = _request(registry=_registry(None), authz_obj=_authz(), tokens=env.tokens, invites=env.invites)

        with (
            patch(f"{MODULE}.discard_tunnel_invites_for_session", env.discard),
            patch(f"{MODULE}.audit_event", env.audit),
            patch("provide.telemetry.get_logger", get_logger),
        ):
            await _handler("tunnels.revoke_token")(req, _TID)

        get_logger.assert_called_once_with(MODULE)

    async def test_the_log_records_a_miss_as_not_found(self) -> None:
        env = await _run("tunnels.revoke_token", session=None, authz_obj=_authz())
        env.logger.info.assert_called_once_with("tunnel_token_revoked session_id=%s found=%s", _TID, False)

    async def test_the_audit_record_names_the_event_principal_session_and_source(self) -> None:
        env = await _run(
            "tunnels.revoke_token",
            session=None,
            authz_obj=_authz(),
            subject_id="dana",
            client_host="10.0.0.9",
        )
        env.audit.assert_called_once_with(
            "tunnel.tokens.revoke",
            principal="dana",
            session_id=_TID,
            source_ip="10.0.0.9",
        )


# ===========================================================================
# rotate_tunnel_tokens
# ===========================================================================


class TestRotateRejections:
    async def test_an_unknown_session_is_a_404_naming_it(self) -> None:
        exc = await _expect_http_error("tunnels.rotate_token", session=None, authz_obj=_authz(admin=True))

        assert exc.status_code == 404
        assert exc.detail == f"unknown session: {_TID}"

    async def test_a_stranger_may_not_rotate(self) -> None:
        exc = await _expect_http_error(
            "tunnels.rotate_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=False, owner=False),
            tokens=_live_tokens(),
        )

        assert exc.status_code == 403
        assert exc.detail == "insufficient privileges"

    async def test_a_session_without_tokens_is_a_distinct_404(self) -> None:
        """Different message from the unknown-session case: the session exists,
        so the caller is told the tunnel was never opened rather than that it
        does not exist."""
        exc = await _expect_http_error(
            "tunnels.rotate_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens={},
        )

        assert exc.status_code == 404
        assert exc.detail == f"no tunnel tokens for {_TID}"

    async def test_a_refused_rotation_leaves_the_existing_tokens_untouched(self) -> None:
        env = _Env()
        env.tokens.update(_live_tokens())
        before = dict(env.tokens[_TID])
        req = _request(
            registry=_registry(SimpleNamespace()),
            authz_obj=_authz(),
            tokens=env.tokens,
            invites=env.invites,
        )

        with pytest.raises(HTTPException):
            await _handler("tunnels.rotate_token")(req, _TID)

        assert env.tokens[_TID] == before


class TestRotateEffects:
    async def test_the_stored_digests_are_replaced(self) -> None:
        env = await _run(
            "tunnels.rotate_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens=_live_tokens(),
        )
        stored = env.tokens[_TID]

        assert stored["worker_token_hash"] == "hash:worker-tok"
        assert stored["share_token_hash"] == "hash:share-tok"
        assert stored["control_token_hash"] == "hash:control-tok"

    async def test_no_plaintext_token_is_stored(self) -> None:
        env = await _run(
            "tunnels.rotate_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens=_live_tokens(),
        )
        assert not {v for v in env.tokens[_TID].values() if v in _TOKENS}

    async def test_the_lease_restarts_from_now_on_the_server_default(self) -> None:
        env = await _run(
            "tunnels.rotate_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens=_live_tokens(),
        )

        assert env.tokens[_TID]["created_at"] == _NOW
        assert env.tokens[_TID]["expires_at"] == _NOW + _TTL
        assert env.result["expires_at"] == _NOW + _TTL

    async def test_the_tunnel_type_carries_over_from_the_previous_tokens(self) -> None:
        env = await _run(
            "tunnels.rotate_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens=_live_tokens(),
        )

        assert env.tokens[_TID]["tunnel_type"] == "http"
        assert env.tokens[_TID]["share_page"] == "inspect"

    async def test_a_previous_record_without_a_type_falls_back_to_terminal(self) -> None:
        env = await _run(
            "tunnels.rotate_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens={_TID: {}},
        )

        assert env.tokens[_TID]["tunnel_type"] == "terminal"
        assert env.tokens[_TID]["share_page"] == "session"

    async def test_the_source_ip_is_rebound_only_when_binding_is_on(self) -> None:
        off = await _run(
            "tunnels.rotate_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens=_live_tokens(),
            client_host="10.0.0.9",
        )
        on = await _run(
            "tunnels.rotate_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens=_live_tokens(),
            config=_config(ip_binding=True),
            client_host="10.0.0.9",
        )

        assert off.tokens[_TID]["issued_ip"] is None
        assert on.tokens[_TID]["issued_ip"] == "10.0.0.9"

    async def test_the_old_invites_are_discarded_before_new_ones_are_issued(self) -> None:
        """Order is load-bearing: issuing first would let the sweep drop the
        invites that were just minted."""
        order: list[str] = []
        env = _Env()
        env.tokens.update(_live_tokens())
        env.discard.side_effect = lambda *_a, **_k: order.append("discard")
        env.issue.side_effect = lambda *_a, **_k: (order.append("issue"), ("S", "C"))[1]
        req = _request(
            registry=_registry(SimpleNamespace()),
            authz_obj=_authz(admin=True),
            tokens=env.tokens,
            invites=env.invites,
        )

        patchers = _patches(env)
        for p in patchers:
            p.start()
        try:
            await _handler("tunnels.rotate_token")(req, _TID)
        finally:
            for p in patchers:
                p.stop()

        assert order == ["discard", "issue"]

    async def test_the_invite_issuer_receives_the_new_tokens_and_deadline(self) -> None:
        env = await _run(
            "tunnels.rotate_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens=_live_tokens(),
        )

        assert env.issue.call_args.kwargs == {
            "session_id": _TID,
            "share_token": "share-tok",
            "control_token": "control-tok",
            "tunnel_expires_at": _NOW + _TTL,
            "issued_ip": None,
            "now": _NOW,
        }


class TestRotateArgumentForwarding:
    """Every collaborator must be handed the real subject, session and store."""

    async def test_the_named_tunnel_is_the_one_looked_up(self) -> None:
        env = _Env()
        env.tokens.update(_live_tokens())
        req = _request(
            registry=_registry(SimpleNamespace()),
            authz_obj=_authz(admin=True),
            tokens=env.tokens,
            invites=env.invites,
        )

        patchers = _patches(env)
        for p in patchers:
            p.start()
        try:
            await _handler("tunnels.rotate_token")(req, _TID)
        finally:
            for p in patchers:
                p.stop()

        req.app.state.uterm_registry.get_definition.assert_awaited_once_with(_TID)

    async def test_ownership_is_checked_for_this_principal_against_this_session(self) -> None:
        session = SimpleNamespace(name="definition")
        az = _authz(admin=False, owner=True)
        env = _Env()
        env.tokens.update(_live_tokens())
        req = _request(registry=_registry(session), authz_obj=az, tokens=env.tokens, invites=env.invites)
        principal = req.state.uterm_principal

        patchers = _patches(env)
        for p in patchers:
            p.start()
        try:
            await _handler("tunnels.rotate_token")(req, _TID)
        finally:
            for p in patchers:
                p.stop()

        az.is_admin.assert_awaited_once_with(principal)
        az.is_owner.assert_awaited_once_with(principal, session)

    async def test_the_invite_store_itself_is_swept_and_reissued_into(self) -> None:
        """Both calls take the live store: handed None or a fresh dict, the old
        invites survive the rotation and keep granting the revoked tokens."""
        env = await _run(
            "tunnels.rotate_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens=_live_tokens(),
        )

        env.discard.assert_called_once_with(env.invites, _TID)
        assert env.issue.call_args.args == (env.invites,)

    async def test_three_independent_tokens_are_minted_at_full_length(self) -> None:
        env = _Env()
        env.tokens.update(_live_tokens())
        req = _request(
            registry=_registry(SimpleNamespace()),
            authz_obj=_authz(admin=True),
            tokens=env.tokens,
            invites=env.invites,
        )

        with (
            patch(f"{MODULE}.time.time", return_value=_NOW),
            patch("secrets.token_urlsafe", side_effect=list(_TOKENS)) as mint,
            patch(f"{MODULE}.hash_token", side_effect=lambda tok: f"hash:{tok}"),
            patch(f"{MODULE}.discard_tunnel_invites_for_session", env.discard),
            patch(f"{MODULE}.issue_tunnel_invites", env.issue),
            patch(f"{MODULE}.audit_event", env.audit),
        ):
            await _handler("tunnels.rotate_token")(req, _TID)

        assert [call.args for call in mint.call_args_list] == [(32,), (32,), (32,)]

    async def test_the_bound_ip_reaches_the_invite_issuer(self) -> None:
        env = await _run(
            "tunnels.rotate_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens=_live_tokens(),
            config=_config(ip_binding=True),
            client_host="10.0.0.9",
        )

        assert env.issue.call_args.kwargs["issued_ip"] == "10.0.0.9"


class TestRotateObservabilityAndResponse:
    async def test_only_the_trailing_slash_is_stripped_not_arbitrary_characters(self) -> None:
        """``rstrip("/")`` takes a character SET; a mutated set eats real
        hostname characters. A host ending in ``X`` proves the set is exactly "/"."""
        env = await _run(
            "tunnels.rotate_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens=_live_tokens(),
            base_url="http://hostX/",
        )

        assert env.result["ws_endpoint"] == f"ws://hostX/tunnel/{_TID}"

    async def test_the_logger_is_named_for_this_module(self) -> None:
        logger = MagicMock(name="logger")
        get_logger = MagicMock(return_value=logger)
        env = _Env()
        env.tokens.update(_live_tokens())
        req = _request(
            registry=_registry(SimpleNamespace()),
            authz_obj=_authz(admin=True),
            tokens=env.tokens,
            invites=env.invites,
        )

        with (
            patch(f"{MODULE}.time.time", return_value=_NOW),
            patch("secrets.token_urlsafe", side_effect=list(_TOKENS)),
            patch(f"{MODULE}.hash_token", side_effect=lambda tok: f"hash:{tok}"),
            patch(f"{MODULE}.discard_tunnel_invites_for_session", env.discard),
            patch(f"{MODULE}.issue_tunnel_invites", env.issue),
            patch(f"{MODULE}.audit_event", env.audit),
            patch("provide.telemetry.get_logger", get_logger),
        ):
            await _handler("tunnels.rotate_token")(req, _TID)

        get_logger.assert_called_once_with(MODULE)

    async def test_the_rotation_is_logged_with_the_session_and_source(self) -> None:
        env = await _run(
            "tunnels.rotate_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens=_live_tokens(),
            client_host="10.0.0.9",
        )
        env.logger.info.assert_called_once_with("tunnel_token_rotated session_id=%s source_ip=%s", _TID, "10.0.0.9")

    async def test_the_audit_record_names_the_event_principal_session_and_source(self) -> None:
        env = await _run(
            "tunnels.rotate_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens=_live_tokens(),
            subject_id="erin",
            client_host="10.0.0.9",
        )
        env.audit.assert_called_once_with(
            "tunnel.tokens.rotate",
            principal="erin",
            session_id=_TID,
            source_ip="10.0.0.9",
        )

    async def test_the_response_carries_the_new_worker_token_and_invite_urls(self) -> None:
        env = await _run(
            "tunnels.rotate_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens=_live_tokens(),
        )

        assert env.result == {
            "tunnel_id": _TID,
            "ws_endpoint": f"ws://host/tunnel/{_TID}",
            "worker_token": "worker-tok",
            "share_url": f"http://host/s/{_TID}?invite=SHARE-INV",
            "control_url": f"http://host/s/{_TID}?invite=CTRL-INV",
            "expires_at": _NOW + _TTL,
        }

    async def test_a_configured_public_base_url_wins_over_the_request(self) -> None:
        env = await _run(
            "tunnels.rotate_token",
            session=SimpleNamespace(),
            authz_obj=_authz(admin=True),
            tokens=_live_tokens(),
            config=_config(public_base_url="https://public.example"),
        )

        assert env.result["ws_endpoint"] == f"wss://public.example/tunnel/{_TID}"
        assert env.result["share_url"].startswith("https://public.example/s/")
