#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for the browser-role resolver the factory hands the hub.

``_resolve_browser_role`` decides what a connecting browser is allowed to be,
and its answer is the role every later redaction and authorization decision is
scoped to. It is a closure inside ``create_server_app``, so its mutants were
attributed to the factory and none of the perimeter reached them.

The part that carries the security is the **unregistered worker**. A worker
that connected ad-hoc has no ``SessionDefinition``, so there is no visibility
policy to consult and the resolver fails closed: only a global admin may
observe it. An operator explicitly opting in via
``auth.allow_adhoc_browser_observers`` widens that to operator/viewer. Each of
those three outcomes is a different level of access to somebody's terminal, and
the refusal is a ``WebSocketException`` with code 1008 -- the frontend routes on
that code, so a changed code is a changed contract.

For a registered session the resolver defers twice, and both must happen: the
authorization check gates access at all, and only then does the policy decide
which role. Skipping the first hands the policy a caller it has already been
told to refuse.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketException, status

from provide.uterm.server import authorization, create_server_app, default_server_config
from provide.uterm.server.app import factory_impl
from provide.uterm.server.auth import Principal

_WORKER = "w1"


def _app(*, allow_adhoc: bool = False) -> Any:
    config = default_server_config()
    config.auth.allow_adhoc_browser_observers = allow_adhoc
    return create_server_app(config, api_only=True)


def _resolver(app: Any) -> Any:
    """The closure the factory handed the hub."""
    return app.state.uterm_hub._resolve_browser_role


def _ws(principal: Principal | None) -> Any:
    ws = MagicMock()
    if principal is None:
        del ws.state.uterm_principal
    else:
        ws.state.uterm_principal = principal
    return ws


def _principal(*roles: str) -> Principal:
    return Principal(subject_id="someone", roles=frozenset(roles), scopes=frozenset())


class _Authz:
    """Stand-in for AuthorizationService — the real one's methods are read-only."""

    def __init__(self, *, can_read: bool) -> None:
        self.can_read = can_read
        self.asked: list[tuple[Any, Any]] = []

    async def can_read_session(self, principal: Any, session: Any) -> bool:
        self.asked.append((principal, session))
        return self.can_read

    async def is_admin(self, _principal: Any) -> bool:
        return False

    async def aclose(self) -> None:
        return None


class _Policy:
    """Stand-in for SessionPolicyResolver."""

    def __init__(self, *, role: str) -> None:
        self.role = role
        self.asked: list[tuple[Any, Any]] = []

    async def role_for(self, principal: Any, session: Any) -> str:
        self.asked.append((principal, session))
        return self.role


def _app_with(
    monkeypatch: pytest.MonkeyPatch, *, can_read: bool, role: str = "viewer", session: Any = None
) -> tuple[Any, _Authz, _Policy]:
    """An app whose authz/policy/registry answers are pinned by the caller."""
    authz, policy = _Authz(can_read=can_read), _Policy(role=role)
    # AuthorizationService is imported inside create_server_app, so the patch has
    # to land on the module it is imported FROM, not on factory_impl.
    monkeypatch.setattr(authorization, "AuthorizationService", lambda *a, **k: authz)
    monkeypatch.setattr(factory_impl, "SessionPolicyResolver", lambda *a, **k: policy)
    app = _app()
    monkeypatch.setattr(app.state.uterm_registry, "get_definition", AsyncMock(return_value=session))
    return app, authz, policy


# ---------------------------------------------------------------------------
# The unregistered worker — fail closed
# ---------------------------------------------------------------------------


async def test_an_admin_may_observe_an_unregistered_worker() -> None:
    """No visibility policy exists for an ad-hoc worker, so only admin passes."""
    app = _app()

    assert await _resolver(app)(_ws(_principal("admin")), _WORKER) == "admin"


@pytest.mark.parametrize("role", ["operator", "viewer"])
async def test_a_non_admin_is_refused_an_unregistered_worker_by_default(role: str) -> None:
    """Failing closed is the default; the refusal code is what the frontend routes on."""
    app = _app()

    with pytest.raises(WebSocketException) as refusal:
        await _resolver(app)(_ws(_principal(role)), _WORKER)

    assert refusal.value.code == status.WS_1008_POLICY_VIOLATION
    assert refusal.value.reason == "insufficient privileges"


async def test_an_operator_opt_in_admits_an_operator_as_an_operator() -> None:
    """The opt-in widens observation, and it must not silently downgrade the role."""
    app = _app(allow_adhoc=True)

    assert await _resolver(app)(_ws(_principal("operator")), _WORKER) == "operator"


async def test_an_operator_opt_in_admits_everyone_else_only_as_a_viewer() -> None:
    """The fall-through: opting in must not promote a viewer to an operator."""
    app = _app(allow_adhoc=True)

    assert await _resolver(app)(_ws(_principal("viewer")), _WORKER) == "viewer"


async def test_an_admin_is_still_an_admin_under_the_opt_in() -> None:
    """The admin arm is checked before the opt-in, not folded into it."""
    app = _app(allow_adhoc=True)

    assert await _resolver(app)(_ws(_principal("admin")), _WORKER) == "admin"


# ---------------------------------------------------------------------------
# A registered session — authorize, then decide the role
# ---------------------------------------------------------------------------


async def test_a_reader_who_is_refused_the_session_never_reaches_the_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two deferrals, in order. The policy must not be asked about a refused caller."""
    app, _authz, policy = _app_with(monkeypatch, can_read=False, session=MagicMock())

    with pytest.raises(WebSocketException) as refusal:
        await _resolver(app)(_ws(_principal("viewer")), _WORKER)

    assert refusal.value.code == status.WS_1008_POLICY_VIOLATION
    assert policy.asked == [], "a refused caller was still handed to the policy"


async def test_an_authorized_reader_gets_the_role_the_policy_assigns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The policy's answer is returned as-is — not the caller's own role claim."""
    app, _authz, _policy = _app_with(monkeypatch, can_read=True, role="operator", session=MagicMock())

    assert await _resolver(app)(_ws(_principal("viewer")), _WORKER) == "operator"


async def test_the_policy_is_asked_about_this_caller_and_this_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both arguments identify what is being decided; either one wrong decides
    something else."""
    session = MagicMock()
    app, authz, policy = _app_with(monkeypatch, can_read=True, session=session)
    principal = _principal("viewer")

    await _resolver(app)(_ws(principal), _WORKER)

    assert authz.asked == [(principal, session)]
    assert policy.asked == [(principal, session)]


async def test_the_session_looked_up_is_the_one_the_browser_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker id travels from the socket to the registry unchanged."""
    app = _app()
    get_definition = AsyncMock(return_value=None)
    monkeypatch.setattr(app.state.uterm_registry, "get_definition", get_definition)

    await _resolver(app)(_ws(_principal("admin")), "some-other-worker")

    get_definition.assert_awaited_once_with("some-other-worker")


# ---------------------------------------------------------------------------
# Where the principal comes from
# ---------------------------------------------------------------------------


async def test_a_principal_already_on_the_socket_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_require_authenticated`` ran first; re-resolving would discard its answer."""
    app = _app()
    resolver = AsyncMock(return_value=_principal("viewer"))
    monkeypatch.setattr(factory_impl, "resolve_ws_principal", resolver)

    assert await _resolver(app)(_ws(_principal("admin")), _WORKER) == "admin"
    resolver.assert_not_awaited()


async def test_a_socket_with_no_principal_is_resolved_through_the_local_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default IDP is local; the resolver is the WS-specific one, with this config."""
    app = _app()
    resolver = AsyncMock(return_value=_principal("admin"))
    monkeypatch.setattr(factory_impl, "resolve_ws_principal", resolver)
    ws = _ws(None)

    assert await _resolver(app)(ws, _WORKER) == "admin"
    resolver.assert_awaited_once_with(ws, app.state.uterm_config.auth)


async def test_a_custom_provider_that_recognises_nobody_yields_an_anonymous_viewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``None`` is an unauthenticated caller, and a viewer is refused an ad-hoc worker.

    Substituting a principal with no roles, or skipping the substitution and
    dereferencing ``None``, both turn a clean refusal into something else.
    """

    class _RecognisesNobody:
        async def resolve_principal(self, _connection: Any) -> Principal | None:
            return None

    monkeypatch.setattr(factory_impl, "build_identity_provider", lambda *a, **k: _RecognisesNobody())
    app = _app()

    with pytest.raises(WebSocketException) as refusal:
        await _resolver(app)(_ws(None), _WORKER)

    assert refusal.value.code == status.WS_1008_POLICY_VIOLATION


async def test_a_custom_provider_principal_is_the_one_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other arm, so "always anonymous" cannot pass."""

    class _RecognisesAnAdmin:
        async def resolve_principal(self, _connection: Any) -> Principal | None:
            return _principal("admin")

    monkeypatch.setattr(factory_impl, "build_identity_provider", lambda *a, **k: _RecognisesAnAdmin())
    app = _app()

    assert await _resolver(app)(_ws(None), _WORKER) == "admin"


# ---------------------------------------------------------------------------
# The fallback principal this resolver builds for itself
# ---------------------------------------------------------------------------


async def test_an_unrecognised_socket_falls_back_to_a_scopeless_anonymous_viewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This resolver builds its own fallback, separate from the auth dependency's.

    Its fields decide what happens next: ``viewer`` is what the ad-hoc guard
    refuses, and an empty scope set is what stops the fallback carrying
    authority nobody granted. A status code cannot see any of that -- the
    refusal looks identical however the principal was built.
    """
    made: list[dict[str, Any]] = []
    real = factory_impl.Principal

    def _record(**kwargs: Any) -> Any:
        made.append(kwargs)
        return real(**kwargs)

    class _RecognisesNobody:
        async def resolve_principal(self, _connection: Any) -> Principal | None:
            return None

    monkeypatch.setattr(factory_impl, "build_identity_provider", lambda *a, **k: _RecognisesNobody())
    monkeypatch.setattr(factory_impl, "Principal", _record)
    app = _app()

    with pytest.raises(WebSocketException):
        await _resolver(app)(_ws(None), _WORKER)

    assert {
        "subject_id": "anonymous",
        "roles": frozenset({"viewer"}),
        "scopes": frozenset(),
    } in made


# ---------------------------------------------------------------------------
# The fan-out controller's authorization callbacks
# ---------------------------------------------------------------------------


async def test_the_fanout_session_check_defers_to_this_servers_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A separate callback from the browser path, asking the same service.

    Wired to the wrong service -- or answering a constant -- it decides fan-out
    membership without consulting the policy that governs the session.
    """
    app, authz, _policy = _app_with(monkeypatch, can_read=False)
    controller = app.state.uterm_hub.fan_out_controller
    principal, session = _principal("viewer"), MagicMock()

    assert await controller._can_read_session(principal, session) is False
    assert authz.asked == [(principal, session)]

    authz.can_read = True
    assert await controller._can_read_session(principal, session) is True


async def test_the_fanout_admin_check_defers_to_this_servers_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Global-admin is a different question from session access, and has its own call."""
    app, authz, _policy = _app_with(monkeypatch, can_read=True)
    controller = app.state.uterm_hub.fan_out_controller

    assert await controller._is_global_admin(_principal("viewer")) is False

    authz.is_admin = AsyncMock(return_value=True)  # type: ignore[method-assign]
    assert await controller._is_global_admin(_principal("admin")) is True


async def test_the_fanout_session_lookup_asks_for_the_worker_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker id has to survive the hop into the registry.

    Asserting only that *a* session comes back cannot see the id being dropped
    -- a stub returns the same object whatever it is asked for, and so does a
    single-session registry. The argument is the assertion.
    """
    session = MagicMock()
    app, _authz, _policy = _app_with(monkeypatch, can_read=True, session=session)
    lookup = AsyncMock(return_value=session)
    monkeypatch.setattr(app.state.uterm_registry, "get_definition", lookup)
    controller = app.state.uterm_hub.fan_out_controller

    assert await controller._resolve_session("some-worker") is session
    lookup.assert_awaited_once_with("some-worker")
