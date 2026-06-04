#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing suite for :class:`StateStore`'s role/policy surface.

Covers the three branch-heavy methods that the incidental hub suites leave
mostly unbound — ``resolve_role_for_browser`` (resolver validation + async
timeout + error re-raise), ``prepare_policy_context`` (principal resolution,
role mapping, JSON-safe metadata projection) and ``_map_roles`` (delegate vs
claims-based mapping). Driven against a hand-written fake hub so every branch
is reachable deterministically without a full hub or a real IdP.

Split from ``test_store_kill.py`` to keep both files under the 500-LOC limit.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import WebSocketException

from provide.uterm.server.bridge.hub.core import BrowserRoleResolutionError
from provide.uterm.server.bridge.hub.store import StateStore
from provide.uterm.server.bridge.identity import Principal
from provide.uterm.server.bridge.models import WorkerTermState


class _FakeRegistry:
    def __init__(self) -> None:
        self._workers: dict[str, Any] = {}

    def get(self, worker_id: str) -> Any:
        return self._workers.get(worker_id)


class _FakeHub:
    def __init__(
        self,
        *,
        on_metric: Any = None,
        resolve_browser_role: Any = None,
        identity_provider: Any = None,
        delegate_roles: bool = True,
    ) -> None:
        self._lock = asyncio.Lock()
        self.registry = _FakeRegistry()
        self._on_metric = on_metric
        self._resolve_browser_role = resolve_browser_role
        self._identity_provider = identity_provider
        self._delegate_roles = delegate_roles


def _store(**kw: Any) -> tuple[StateStore, _FakeHub]:
    hub = _FakeHub(**kw)
    return StateStore(hub), hub


class _FakeWS:
    """Hashable WebSocket stand-in (SimpleNamespace defines __eq__ ⇒ unhashable)."""

    def __init__(self, *, state: Any = None) -> None:
        self.state = state


def _ws_with_principal(principal: Any) -> _FakeWS:
    return _FakeWS(state=SimpleNamespace(uterm_principal=principal))


# == resolve_role_for_browser ================================================


async def test_resolve_role_no_resolver_defaults_viewer() -> None:
    store, _ = _store(resolve_browser_role=None)
    assert await store.resolve_role_for_browser(object(), "w") == "viewer"


@pytest.mark.parametrize("role", ["viewer", "operator", "admin"])
async def test_resolve_role_sync_valid_role_passthrough(role: str) -> None:
    store, _ = _store(resolve_browser_role=lambda ws, wid: role)
    assert await store.resolve_role_for_browser(object(), "w") == role


async def test_resolve_role_passes_ws_and_worker_id_to_resolver() -> None:
    """The resolver is invoked with the real ws and worker_id (pins both args)."""
    seen: list[tuple[Any, str]] = []

    def _resolver(ws: Any, wid: str) -> str:
        seen.append((ws, wid))
        return "operator"

    store, _ = _store(resolve_browser_role=_resolver)
    ws = object()
    assert await store.resolve_role_for_browser(ws, "w") == "operator"
    assert seen == [(ws, "w")]


async def test_resolve_role_viewer_result_takes_valid_branch_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    """A "viewer" result is a VALID role → returned via the valid branch, no warning.

    Pins the literal members of the ``{"viewer", "operator", "admin"}`` set: a
    mutated "viewer" member would push this through the invalid-role branch and
    emit a warning (the output stays "viewer" either way, so only the log differs).
    """
    store, _ = _store(resolve_browser_role=lambda ws, wid: "viewer")
    with caplog.at_level(logging.WARNING, logger="provide.uterm.server.bridge.hub"):
        out = await store.resolve_role_for_browser(object(), "w")
    assert out == "viewer"
    assert not any("resolve_browser_role_invalid" in r.getMessage() for r in caplog.records)


async def test_resolve_role_sync_invalid_role_falls_back_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    store, _ = _store(resolve_browser_role=lambda ws, wid: "root")
    with caplog.at_level(logging.WARNING, logger="provide.uterm.server.bridge.hub"):
        out = await store.resolve_role_for_browser(object(), "w")
    assert out == "viewer"  # not in {viewer,operator,admin} → default
    # Exact message pins the worker_id + role args and the format string.
    assert any(
        "resolve_browser_role_invalid worker_id=w role='root' [provide.uterm.server.bridge.hub.store]" in r.getMessage()
        for r in caplog.records
    )


async def test_resolve_role_none_result_no_invalid_warning(caplog: pytest.LogCaptureFixture) -> None:
    """A None resolver result falls back to viewer WITHOUT the invalid-role warning."""
    store, _ = _store(resolve_browser_role=lambda ws, wid: None)
    with caplog.at_level(logging.WARNING, logger="provide.uterm.server.bridge.hub"):
        out = await store.resolve_role_for_browser(object(), "w")
    assert out == "viewer"
    assert not any("resolve_browser_role_invalid" in r.message for r in caplog.records)


async def test_resolve_role_async_valid() -> None:
    async def _resolver(ws: Any, wid: str) -> str:
        return "admin"

    store, _ = _store(resolve_browser_role=_resolver)
    assert await store.resolve_role_for_browser(object(), "w") == "admin"


async def test_resolve_role_async_timeout_raises_and_meters(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An awaitable resolver that exceeds the 5s wait raises + emits the timeout metric."""
    metrics: list[str] = []
    timeouts: list[float] = []

    async def _hangs(ws: Any, wid: str) -> str:
        await asyncio.sleep(3600)
        return "admin"  # pragma: no cover

    async def _instant_timeout(awaitable: Any, timeout: float) -> Any:
        timeouts.append(timeout)  # record the wait deadline (pins timeout=5.0)
        if hasattr(awaitable, "close"):
            awaitable.close()  # avoid "coroutine never awaited"
        raise TimeoutError

    from provide.uterm.server.bridge.hub import store as store_module

    monkeypatch.setattr(store_module.asyncio, "wait_for", _instant_timeout)
    store, _ = _store(resolve_browser_role=_hangs, on_metric=lambda name, value: metrics.append(name))

    with caplog.at_level(logging.WARNING, logger="provide.uterm.server.bridge.hub"):
        with pytest.raises(BrowserRoleResolutionError) as excinfo:
            await store.resolve_role_for_browser(object(), "w")
    assert timeouts == [5.0]  # the hardcoded 5s deadline
    assert excinfo.value.args == ("w",)  # error carries the worker_id, not None
    assert "browser_role_resolution_timeout" in metrics
    assert any(
        "resolve_browser_role_timeout worker_id=w [provide.uterm.server.bridge.hub.store]" in r.getMessage()
        for r in caplog.records
    )


async def test_resolve_role_generic_exception_wrapped(caplog: pytest.LogCaptureFixture) -> None:
    def _boom(ws: Any, wid: str) -> str:
        raise ValueError("nope")

    store, _ = _store(resolve_browser_role=_boom)
    with caplog.at_level(logging.WARNING, logger="provide.uterm.server.bridge.hub"):
        with pytest.raises(BrowserRoleResolutionError) as excinfo:
            await store.resolve_role_for_browser(object(), "w")
    assert excinfo.value.args == ("w",)  # error carries the worker_id, not None
    # Exact message pins the worker_id + error args and the format string.
    assert any(
        "resolve_browser_role_failed worker_id=w error=nope [provide.uterm.server.bridge.hub.store]" in r.getMessage()
        for r in caplog.records
    )


async def test_resolve_role_websocket_exception_reraised() -> None:
    """WebSocketException is re-raised as-is, NOT wrapped in BrowserRoleResolutionError."""

    def _ws_exc(ws: Any, wid: str) -> str:
        raise WebSocketException(code=1008, reason="denied")

    store, _ = _store(resolve_browser_role=_ws_exc)
    with pytest.raises(WebSocketException):
        await store.resolve_role_for_browser(object(), "w")


async def test_resolve_role_resolution_error_reraised() -> None:
    """An already-typed BrowserRoleResolutionError propagates unchanged (not re-wrapped)."""

    def _re(ws: Any, wid: str) -> str:
        raise BrowserRoleResolutionError("w")

    store, _ = _store(resolve_browser_role=_re)
    with pytest.raises(BrowserRoleResolutionError):
        await store.resolve_role_for_browser(object(), "w")


# == _map_roles ==============================================================


def test_map_roles_delegate_uses_principal_roles() -> None:
    store, _ = _store(delegate_roles=True)
    p = Principal(subject_id="u", roles=frozenset({"admin", "ops"}))
    assert store._map_roles(p) == frozenset({"admin", "ops"})


def test_map_roles_delegate_empty_roles_defaults_viewer() -> None:
    store, _ = _store(delegate_roles=True)
    assert store._map_roles(Principal(subject_id="u", roles=frozenset())) == frozenset({"viewer"})


def test_map_roles_claims_admin() -> None:
    store, _ = _store(delegate_roles=False)
    assert store._map_roles(Principal(subject_id="u", claims={"admin": True})) == frozenset({"admin"})


def test_map_roles_claims_is_admin_alias() -> None:
    """The ``admin`` claim OR the ``is_admin`` claim grants admin (pins the ``or``)."""
    store, _ = _store(delegate_roles=False)
    assert store._map_roles(Principal(subject_id="u", claims={"is_admin": True})) == frozenset({"admin"})


def test_map_roles_claims_operator() -> None:
    store, _ = _store(delegate_roles=False)
    assert store._map_roles(Principal(subject_id="u", claims={"operator": True})) == frozenset({"operator"})


def test_map_roles_admin_beats_operator() -> None:
    """admin + operator both present → admin (pins the elif over a second if)."""
    store, _ = _store(delegate_roles=False)
    p = Principal(subject_id="u", claims={"admin": True, "operator": True})
    assert store._map_roles(p) == frozenset({"admin"})


def test_map_roles_no_claims_defaults_viewer() -> None:
    store, _ = _store(delegate_roles=False)
    assert store._map_roles(Principal(subject_id="u", claims={})) == frozenset({"viewer"})


def test_map_roles_none_claims_defaults_viewer() -> None:
    """``principal.claims or {}`` tolerates a falsy claims object → viewer."""
    store, _ = _store(delegate_roles=False)
    p = Principal(subject_id="u")
    object.__setattr__(p, "claims", None)  # simulate a falsy claims attribute
    assert store._map_roles(p) == frozenset({"viewer"})


def test_map_roles_delegate_missing_roles_attr_defaults_viewer() -> None:
    """A principal lacking a ``roles`` attribute resolves to viewer, not an error.

    Pins ``getattr(principal, "roles", None)`` — without the None default a
    principal with no ``roles`` attribute would raise AttributeError.
    """
    store, _ = _store(delegate_roles=True)
    assert store._map_roles(SimpleNamespace(subject_id="u")) == frozenset({"viewer"})


# == prepare_policy_context ==================================================


async def test_prepare_context_no_principal_uses_browser_role() -> None:
    store, hub = _store()
    ws = _FakeWS(state=SimpleNamespace())  # no uterm_principal attribute
    st = WorkerTermState()
    st.browsers = {ws: "operator"}
    hub.registry._workers["w"] = st

    ctx = await store.prepare_policy_context(ws, "w", action="send")
    assert ctx.worker_id == "w"
    assert ctx.role == "operator"  # from st.browsers, no principal override
    assert ctx.client_id == "anonymous"
    assert ctx.action == "send"
    assert ctx.metadata == {}


async def test_prepare_context_missing_worker_role_none() -> None:
    store, _ = _store()
    ws = _FakeWS(state=SimpleNamespace())
    ctx = await store.prepare_policy_context(ws, "ghost")
    assert ctx.role is None  # st is None → role stays None


async def test_prepare_context_ws_without_state_attr_is_clean() -> None:
    """A ws lacking a ``state`` attribute resolves to no principal, not an error.

    Pins the inner ``getattr(ws, "state", None)`` default — without it a ws with
    no ``state`` attribute would raise AttributeError.
    """
    store, _ = _store()
    ctx = await store.prepare_policy_context(object(), "ghost")
    assert ctx.role is None
    assert ctx.client_id == "anonymous"
    assert ctx.metadata == {}


async def test_prepare_context_principal_admin_overrides_role_and_projects_metadata() -> None:
    store, hub = _store()
    principal = Principal(subject_id="u-1", roles=frozenset({"admin", "viewer"}))
    ws = _ws_with_principal(principal)
    st = WorkerTermState()
    st.browsers = {ws: "viewer"}
    hub.registry._workers["w"] = st

    ctx = await store.prepare_policy_context(ws, "w")
    assert ctx.role == "admin"  # principal admin overrides the browser viewer role
    assert ctx.client_id == "u-1"
    # JSON-safe projection: ONLY subject_id + sorted roles.
    assert ctx.metadata == {"principal": {"subject_id": "u-1", "roles": ["admin", "viewer"]}}


async def test_prepare_context_principal_operator_role() -> None:
    store, _ = _store()
    ws = _ws_with_principal(Principal(subject_id="u", roles=frozenset({"operator"})))
    ctx = await store.prepare_policy_context(ws, "w")
    assert ctx.role == "operator"


async def test_prepare_context_principal_viewer_role() -> None:
    """A principal mapping to only viewer takes the ``else`` branch → viewer."""
    store, _ = _store()
    ws = _ws_with_principal(Principal(subject_id="u", roles=frozenset({"viewer"})))
    ctx = await store.prepare_policy_context(ws, "w")
    assert ctx.role == "viewer"


async def test_prepare_context_string_principal_branch() -> None:
    """A bare string principal does not map roles; metadata stringifies it."""
    store, hub = _store()
    ws = _ws_with_principal("anon-token")
    st = WorkerTermState()
    st.browsers = {ws: "operator"}
    hub.registry._workers["w"] = st

    ctx = await store.prepare_policy_context(ws, "w")
    assert ctx.role == "operator"  # string principal → no role override
    assert ctx.client_id == "anon-token"
    assert ctx.metadata == {"principal": "anon-token"}


async def test_prepare_context_uses_identity_provider_when_present() -> None:
    captured: list[Any] = []

    class _IdP:
        async def resolve_principal(self, ws: Any) -> Any:
            captured.append(ws)
            return Principal(subject_id="idp-user", roles=frozenset({"admin"}))

    store, _ = _store(identity_provider=_IdP())
    ws = _ws_with_principal(Principal(subject_id="ignored", roles=frozenset({"viewer"})))
    ctx = await store.prepare_policy_context(ws, "w")
    assert captured == [ws]  # IdP path taken, not ws.state
    assert ctx.role == "admin"
    assert ctx.client_id == "idp-user"
