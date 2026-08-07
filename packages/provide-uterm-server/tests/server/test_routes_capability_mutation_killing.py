#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing suite for the RouteDefs-era ``*_capability_handlers`` factories.

Separate from test_routes_mutation_killing.py, which covers the decorated-era
surface (module accessors, ``create_*_router`` bodies) and is already near the
777-line cap.

Why these need their own suite: mutmut SKIPS decorated functions, so while the
route handlers sat behind ``@router.get``/``@router.post`` they were never
mutated at all. 9bc4dd0c moved them into undecorated nested defs inside
``*_capability_handlers()`` factories, which made that logic mutable for the
first time — the existing tests execute it (line coverage was, and stayed, 100%)
but assert too little to kill the mutants.

The handlers are called straight off the factory dict rather than pulled from a
router: same function object, no path/route plumbing needed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException


def _request(
    *,
    app_state: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    client_host: str | None = "1.2.3.4",
) -> MagicMock:
    req = MagicMock(name="request")
    req.app.state = SimpleNamespace(**(app_state or {}))
    req.state = SimpleNamespace(**(state or {}))
    req.headers = {}
    req.url = SimpleNamespace(scheme="http")
    req.client = SimpleNamespace(host=client_host) if client_host is not None else None
    return req


# ===========================================================================
# sse.py
# ===========================================================================


def _sse_handler() -> Any:
    from provide.uterm.server.routes.sse import sse_capability_handlers

    return sse_capability_handlers()["sessions.events_stream"]


def _sse_request(*, registry: Any, authz_obj: Any, principal: Any = None) -> MagicMock:
    return _request(
        app_state={"uterm_registry": registry, "uterm_authz": authz_obj},
        state={} if principal is None else {"uterm_principal": principal},
    )


class TestSseCapabilityHandler:
    async def test_unregistered_capability_handler_raises(self) -> None:
        """The placeholder bound to every capability this module does NOT serve.
        Nothing routes to it, so without a test its mutants have no covering test
        at all — mutmut reports them "no tests" and they still count against the
        score. Exact equality, not pytest.raises(match=...): match is a regex
        SEARCH, so a message padded on both ends still matches and survives."""
        from provide.uterm.server.routes.sse import _unregistered_capability_handler

        with pytest.raises(RuntimeError) as exc:
            await _unregistered_capability_handler()
        assert str(exc.value) == "unregistered shared API capability invoked"

    async def test_missing_principal_500(self) -> None:
        ep = _sse_handler()
        req = _sse_request(registry=MagicMock(), authz_obj=MagicMock())
        with pytest.raises(HTTPException) as exc:
            await ep(req, "s1")
        assert exc.value.status_code == 500
        assert exc.value.detail == "principal was not resolved"

    async def test_passes_through_principal_definition_and_session_id(self) -> None:
        """Dropping or nulling any forwarded argument authorizes the wrong
        subject, or streams the wrong session."""
        ep = _sse_handler()
        principal, definition = object(), SimpleNamespace()
        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=definition)

        async def _gen() -> Any:  # pragma: no cover - never iterated here
            yield "x"

        gen = _gen()
        reg.stream_session_events = MagicMock(return_value=gen)
        az = MagicMock()
        az.can_read_session = AsyncMock(return_value=True)

        resp = await ep(_sse_request(registry=reg, authz_obj=az, principal=principal), "s1")
        reg.get_definition.assert_awaited_once_with("s1")
        az.can_read_session.assert_awaited_once_with(principal, definition)
        assert resp.body_iterator is gen

    async def test_sets_the_proxy_buffering_headers(self) -> None:
        """These two headers are what stop a proxy buffering the stream shut.
        Header NAME case is not asserted — Starlette lowercases names, so those
        mutants are equivalent and allowlisted; the VALUES are case-sensitive."""
        ep = _sse_handler()
        reg = MagicMock()
        reg.get_definition = AsyncMock(return_value=SimpleNamespace())

        async def _gen() -> Any:  # pragma: no cover
            yield "x"

        reg.stream_session_events = MagicMock(return_value=_gen())
        az = MagicMock()
        az.can_read_session = AsyncMock(return_value=True)

        resp = await ep(_sse_request(registry=reg, authz_obj=az, principal=object()), "s1")
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"


# ===========================================================================
# sessions.py — table-driven
#
# 19 handlers of near-identical shape (resolve principal, authorize, call one
# registry method, map KeyError to 404). One row per capability with three
# assertions beats 19 hand-written near-copies, and the table itself is checked
# for completeness so a new handler cannot join the factory unmeasured.
#
# Each row pins what a mutant can silently change: WHICH authz method is
# consulted and with WHICH capability string, that a denial is a 403 rather than
# a pass-through, and WHICH registry method receives the session_id.
# ===========================================================================


class _Route(SimpleNamespace):
    capability: str
    authz_method: str
    authz_arg: str | None
    registry_method: str
    maps_keyerror_to_404: bool
    kwargs: dict[str, Any]


def _route(
    capability: str,
    authz_method: str,
    authz_arg: str | None,
    registry_method: str,
    *,
    maps_keyerror_to_404: bool = True,
    **kwargs: Any,
) -> _Route:
    return _Route(
        capability=capability,
        authz_method=authz_method,
        authz_arg=authz_arg,
        registry_method=registry_method,
        maps_keyerror_to_404=maps_keyerror_to_404,
        kwargs=kwargs,
    )


# Capability strings are spelled out on purpose: a mutant rewriting
# "session.control.delete" is only caught because a test asserts the exact
# string the handler passes to the authorizer.
_ROUTES: tuple[_Route, ...] = (
    _route("sessions.get", "can_read_session", None, "get_session"),
    _route("sessions.connect", "can_mutate_session", "session.control.connect", "start_session"),
    _route("sessions.disconnect", "can_mutate_session", "session.control.connect", "stop_session"),
    _route("sessions.restart", "can_mutate_session", "session.control.connect", "restart_session"),
    _route("sessions.clear", "can_mutate_session", "session.control.clear", "clear_session"),
    _route("sessions.analyze", "can_read_session", None, "analyze_session"),
    # delete has no try/except: a KeyError from the registry propagates.
    _route(
        "sessions.delete",
        "can_mutate_session",
        "session.control.delete",
        "delete_session",
        maps_keyerror_to_404=False,
    ),
    _route("sessions.update", "can_mutate_session", "session.control.update", "update_session", payload={}),
    _route(
        "sessions.set_mode",
        "can_mutate_session",
        "session.control.mode",
        "set_mode",
        payload={"input_mode": "open"},
    ),
    _route("sessions.snapshot", "can_read_session", None, "last_snapshot", maps_keyerror_to_404=False),
    _route("sessions.events", "can_read_session", None, "events", maps_keyerror_to_404=False),
    _route("sessions.events_watch", "can_read_session", None, "watch_session_events", maps_keyerror_to_404=False),
    _route("sessions.recording", "can_read_recording", None, "recording_meta"),
    _route("sessions.recording_entries", "can_read_recording", None, "recording_entries"),
    _route("sessions.recording_download", "can_read_recording", None, "recording_path"),
)

_IDS = tuple(r.capability for r in _ROUTES)
_KEYERROR_ROUTES = tuple(r for r in _ROUTES if r.maps_keyerror_to_404)
_KEYERROR_IDS = tuple(r.capability for r in _KEYERROR_ROUTES)

# Handlers taking no session_id, plus annotate (distinct body: event payload +
# timestamp). Listed so the completeness check accounts for every capability.
_NO_SESSION_ID = frozenset({"sessions.list", "sessions.create", "sessions.bulk_delete"})
_SEPARATE = frozenset({"sessions.annotate"})


def _handlers() -> dict[str, Any]:
    from provide.uterm.server.routes.sessions import session_capability_handlers

    return session_capability_handlers()


def _session_request(*, registry: Any, authz_obj: Any, principal: Any = None) -> MagicMock:
    return _request(
        app_state={
            "uterm_registry": registry,
            "uterm_authz": authz_obj,
            "uterm_tunnel_tokens": {},
        },
        state={"uterm_principal": principal if principal is not None else SimpleNamespace(subject_id="u1")},
    )


def _registry_with(definition: Any) -> MagicMock:
    reg = MagicMock()
    reg.get_definition = AsyncMock(return_value=definition)
    return reg


def _authz(route: _Route, *, allow: bool) -> MagicMock:
    az = MagicMock()
    setattr(az, route.authz_method, AsyncMock(return_value=allow))
    az.is_admin = AsyncMock(return_value=True)
    return az


class TestSessionCapabilityHandlers:
    def test_table_covers_every_capability(self) -> None:
        """A handler added to the factory without a row here would be mutation-
        untested exactly the way this whole family already was."""
        accounted = {r.capability for r in _ROUTES} | _NO_SESSION_ID | _SEPARATE
        assert accounted == set(_handlers())

    @pytest.mark.parametrize("route", _ROUTES, ids=_IDS)
    async def test_denied_is_403_with_the_exact_capability_consulted(self, route: _Route) -> None:
        definition = SimpleNamespace()
        principal = SimpleNamespace(subject_id="u1")
        az = _authz(route, allow=False)
        req = _session_request(registry=_registry_with(definition), authz_obj=az, principal=principal)

        with pytest.raises(HTTPException) as exc:
            await _handlers()[route.capability](req, "s1", **route.kwargs)
        assert exc.value.status_code == 403
        assert exc.value.detail == "insufficient privileges"

        args = getattr(az, route.authz_method).await_args.args
        assert args[0] is principal, "the resolved principal must be the subject authorized"
        assert args[1] is definition, "the looked-up definition must be what is authorized against"
        if route.authz_arg is None:
            assert len(args) == 2
        else:
            assert args[2] == route.authz_arg

    @pytest.mark.parametrize("route", _KEYERROR_ROUTES, ids=_KEYERROR_IDS)
    async def test_missing_session_is_404_naming_the_id(self, route: _Route) -> None:
        reg = _registry_with(SimpleNamespace())
        setattr(reg, route.registry_method, AsyncMock(side_effect=KeyError("gone")))
        req = _session_request(registry=reg, authz_obj=_authz(route, allow=True))

        with pytest.raises(HTTPException) as exc:
            await _handlers()[route.capability](req, "s1", **route.kwargs)
        assert exc.value.status_code == 404
        assert exc.value.detail == "unknown session: s1"

    @pytest.mark.parametrize("route", _ROUTES, ids=_IDS)
    async def test_forwards_the_session_id_to_the_registry(self, route: _Route) -> None:
        reg = _registry_with(SimpleNamespace())
        setattr(reg, route.registry_method, AsyncMock(return_value=MagicMock()))
        req = _session_request(registry=reg, authz_obj=_authz(route, allow=True))

        try:
            await _handlers()[route.capability](req, "s1", **route.kwargs)
        except HTTPException as exc:  # recording_download 404s on an absent file
            assert exc.status_code == 404

        called = getattr(reg, route.registry_method)
        called.assert_awaited_once()
        assert called.await_args.args[0] == "s1"
