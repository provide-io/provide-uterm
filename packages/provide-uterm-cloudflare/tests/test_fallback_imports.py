#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for CF-runtime fallback import paths.

Each of session_runtime.py, entry.py, and ui/assets.py has a module-level
try/except that switches between the installed package path and the bare module
path used inside Cloudflare Workers Python runtime:

    try:
        from provide.terminal.cloudflare.X import Y   # installed package path
    except Exception:
        from X import Y                              # CF Workers flat path

This file exercises the bare-module branch by temporarily blocking the primary
import path and injecting mock flat modules, then re-importing the target.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from types import ModuleType

_MISSING = object()  # sentinel for "key was absent from sys.modules"


@contextmanager
def _patched_sys_modules(
    block: list[str],
    inject: dict[str, object],
) -> Iterator[None]:
    """Temporarily block and/or replace sys.modules entries, then restore."""
    saved: dict[str, object] = {}
    for key in block:
        saved[key] = sys.modules.get(key, _MISSING)
        sys.modules[key] = None  # type: ignore[assignment]
    for key, val in inject.items():
        saved.setdefault(key, sys.modules.get(key, _MISSING))
        sys.modules[key] = val  # type: ignore[assignment]
    try:
        yield
    finally:
        for key in list(block) + list(inject.keys()):
            sys.modules.pop(key, None)
        for key, val in saved.items():
            if val is _MISSING:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = val  # type: ignore[assignment]


def _fresh_import(
    target: str,
    block: list[str],
    inject: dict[str, object],
) -> ModuleType:
    """Remove *target* from sys.modules, block primary imports, inject mocks, re-import."""
    original = sys.modules.pop(target, _MISSING)
    try:
        with _patched_sys_modules(block, inject):
            return importlib.import_module(target)
    finally:
        sys.modules.pop(target, None)
        if original is not _MISSING:
            sys.modules[target] = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# ui/assets.py — lines 8-9
# ---------------------------------------------------------------------------


def test_ui_assets_cf_types_fallback() -> None:
    """ui/assets.py falls back to bare 'cf_types' when package path is unavailable."""
    from provide.terminal.cloudflare.cf_types import Response as _RealResponse

    mock_cf = ModuleType("cf_types")
    mock_cf.Response = _RealResponse  # type: ignore[attr-defined]

    mod = _fresh_import(
        "provide.terminal.cloudflare.ui.assets",
        block=["provide.terminal.cloudflare.cf_types"],
        inject={"cf_types": mock_cf},
    )
    assert mod.Response is _RealResponse


# ---------------------------------------------------------------------------
# entry.py — lines 12-17
# ---------------------------------------------------------------------------


def test_entry_module_level_fallback() -> None:
    """entry.py module-level imports fall back to bare module names."""
    from provide.terminal.cloudflare import config as real_config
    from provide.terminal.cloudflare.auth.jwt import JwtValidationError, decode_jwt, extract_bearer_or_cookie
    from provide.terminal.cloudflare.cf_types import Response, WorkerEntrypoint, json_response
    from provide.terminal.cloudflare.do import session_runtime as real_sr_mod
    from provide.terminal.cloudflare.state import registry as real_reg
    from provide.terminal.cloudflare.ui import assets as real_assets

    cf = ModuleType("cf_types")
    cf.Response = Response  # type: ignore[attr-defined]
    cf.WorkerEntrypoint = WorkerEntrypoint  # type: ignore[attr-defined]
    cf.json_response = json_response  # type: ignore[attr-defined]

    auth_jwt = ModuleType("auth.jwt")
    auth_jwt.JwtValidationError = JwtValidationError  # type: ignore[attr-defined]
    auth_jwt.decode_jwt = decode_jwt  # type: ignore[attr-defined]
    auth_jwt.extract_bearer_or_cookie = extract_bearer_or_cookie  # type: ignore[attr-defined]
    do_sr = ModuleType("do.session_runtime")
    do_sr.SessionRuntime = real_sr_mod.SessionRuntime  # type: ignore[attr-defined]

    workers_stub = ModuleType("workers")
    workers_stub.DurableObject = object  # type: ignore[attr-defined]
    workers_stub.WorkerEntrypoint = WorkerEntrypoint  # type: ignore[attr-defined]
    workers_stub.Response = Response  # type: ignore[attr-defined]

    inject = {
        "auth": ModuleType("auth"),
        "auth.jwt": auth_jwt,
        "cf_types": cf,
        "config": real_config,
        "do": ModuleType("do"),
        "do.session_runtime": do_sr,
        "state": ModuleType("state"),
        "state.registry": real_reg,
        "ui": ModuleType("ui"),
        "ui.assets": real_assets,
        "workers": workers_stub,
    }

    mod = _fresh_import(
        "provide.terminal.cloudflare.entry",
        block=["provide.terminal.cloudflare.cf_types"],
        inject=inject,
    )
    # Default class was built using the fallback WorkerEntrypoint
    assert issubclass(mod.Default, WorkerEntrypoint)


# ---------------------------------------------------------------------------
# entry.py — lines 66-67  (inline import inside Default.fetch)
# ---------------------------------------------------------------------------


async def test_entry_inline_jwt_fallback() -> None:
    """entry.py inline JWT import falls back to bare auth.jwt in CF runtime."""
    from types import ModuleType, SimpleNamespace

    # entry.py may need a fresh import after test_entry_module_level_fallback
    # clears it from sys.modules.  Mock the CF-only `workers` module so the
    # Python-3.12-syntax _workers.py is never parsed on Python ≤ 3.11.
    _workers_stub = ModuleType("workers")
    _workers_stub.DurableObject = object  # type: ignore[attr-defined]
    _workers_stub.WorkerEntrypoint = object  # type: ignore[attr-defined]
    _workers_stub.Response = None  # type: ignore[attr-defined]
    with _patched_sys_modules([], {"workers": _workers_stub}):
        from provide.terminal.cloudflare.entry import Default

    class _FakeJwtError(Exception):
        pass

    async def _always_invalid(token: str, config: object) -> None:
        raise _FakeJwtError("test token rejected")

    from provide.terminal.cloudflare.auth.jwt import extract_bearer_or_cookie

    mock_auth_jwt = ModuleType("auth.jwt")
    mock_auth_jwt.JwtValidationError = _FakeJwtError  # type: ignore[attr-defined]
    mock_auth_jwt.decode_jwt = _always_invalid  # type: ignore[attr-defined]
    mock_auth_jwt.extract_bearer_or_cookie = extract_bearer_or_cookie  # type: ignore[attr-defined]

    env = SimpleNamespace(
        AUTH_MODE="jwt",
        JWT_JWKS_URL=None,
        JWT_AUDIENCE=None,
        JWT_ISSUER=None,
        JWT_DEFAULT_ROLE="viewer",
        JWT_ROLES_CLAIM="role",
        SESSION_REGISTRY=None,
        WORKER_BEARER_TOKEN="test-worker-token",
    )
    default = Default(env)

    class _FakeRequest:
        url = "https://example.com/api/sessions"
        headers = SimpleNamespace(get=lambda k, d=None: "Bearer some-token" if k == "Authorization" else d)

        async def text(self) -> str:
            return "{}"

    with _patched_sys_modules(
        block=["provide.terminal.cloudflare.auth.jwt"],
        inject={"auth": ModuleType("auth"), "auth.jwt": mock_auth_jwt},
    ):
        resp = await default.fetch(_FakeRequest())

    assert resp.status == 401


# ---------------------------------------------------------------------------
# session_runtime.py — lines 21-31
# ---------------------------------------------------------------------------


def test_session_runtime_module_level_fallback() -> None:
    """session_runtime.py module-level imports fall back to bare module names."""
    from provide.terminal.cloudflare.api.http_routes import route_http
    from provide.terminal.cloudflare.api.ws_routes import handle_socket_message
    from provide.terminal.cloudflare.auth.jwt import JwtValidationError, decode_jwt, extract_bearer_or_cookie
    from provide.terminal.cloudflare.auth.jwt import resolve_role as _resolve_jwt_role
    from provide.terminal.cloudflare.bridge.hijack import HijackCoordinator, HijackSession
    from provide.terminal.cloudflare.cf_types import CFWebSocket, DurableObject, Response
    from provide.terminal.cloudflare.config import CloudflareConfig
    from provide.terminal.cloudflare.do.persistence import clear_lease as _clear_lease
    from provide.terminal.cloudflare.do.persistence import persist_lease as _persist_lease
    from provide.terminal.cloudflare.do.session_runtime.ws_helpers import _WsHelperMixin
    from provide.terminal.cloudflare.do.ushell import init_ushell, on_browser_connected
    from provide.terminal.cloudflare.state.registry import KV_REFRESH_S, update_kv_session
    from provide.terminal.cloudflare.state.store import LeaseRecord, SqliteStateStore

    # Build flat mock modules using real classes so base-class inheritance works.
    api_http = ModuleType("api.http_routes")
    api_http.route_http = route_http  # type: ignore[attr-defined]

    api_ws = ModuleType("api.ws_routes")
    api_ws.handle_socket_message = handle_socket_message  # type: ignore[attr-defined]

    auth_jwt = ModuleType("auth.jwt")
    auth_jwt.JwtValidationError = JwtValidationError  # type: ignore[attr-defined]
    auth_jwt.decode_jwt = decode_jwt  # type: ignore[attr-defined]
    auth_jwt.extract_bearer_or_cookie = extract_bearer_or_cookie  # type: ignore[attr-defined]
    auth_jwt.resolve_role = _resolve_jwt_role  # type: ignore[attr-defined]

    bridge_hijack = ModuleType("bridge.hijack")
    bridge_hijack.HijackCoordinator = HijackCoordinator  # type: ignore[attr-defined]
    bridge_hijack.HijackSession = HijackSession  # type: ignore[attr-defined]

    cf = ModuleType("cf_types")
    cf.CFWebSocket = CFWebSocket  # type: ignore[attr-defined]
    cf.DurableObject = DurableObject  # type: ignore[attr-defined]
    cf.Response = Response  # type: ignore[attr-defined]

    _config_mod = ModuleType("config")
    _config_mod.CloudflareConfig = CloudflareConfig  # type: ignore[attr-defined]

    do_persistence = ModuleType("do.persistence")
    do_persistence.clear_lease = _clear_lease  # type: ignore[attr-defined]
    do_persistence.persist_lease = _persist_lease  # type: ignore[attr-defined]

    do_ushell = ModuleType("do.ushell")
    do_ushell.init_ushell = init_ushell  # type: ignore[attr-defined]
    do_ushell.on_browser_connected = on_browser_connected  # type: ignore[attr-defined]

    state_reg = ModuleType("state.registry")
    state_reg.KV_REFRESH_S = KV_REFRESH_S  # type: ignore[attr-defined]
    state_reg.update_kv_session = update_kv_session  # type: ignore[attr-defined]

    state_store = ModuleType("state.store")
    state_store.LeaseRecord = LeaseRecord  # type: ignore[attr-defined]
    state_store.SqliteStateStore = SqliteStateStore  # type: ignore[attr-defined]

    # Note: ``_SessionRuntimeIoMixin`` and ``_WsHelperMixin`` now live as
    # siblings in the ``session_runtime`` package and are pulled in via
    # relative imports — no flat-layout fallback is needed for them, so they
    # are not injected here.
    inject = {
        "api": ModuleType("api"),
        "api.http_routes": api_http,
        "api.ws_routes": api_ws,
        "auth": ModuleType("auth"),
        "auth.jwt": auth_jwt,
        "bridge": ModuleType("bridge"),
        "bridge.hijack": bridge_hijack,
        "cf_types": cf,
        "config": _config_mod,
        "do": ModuleType("do"),
        "do.persistence": do_persistence,
        "do.ushell": do_ushell,
        "state": ModuleType("state"),
        "state.registry": state_reg,
        "state.store": state_store,
    }

    mod = _fresh_import(
        "provide.terminal.cloudflare.do.session_runtime",
        block=["provide.terminal.cloudflare.api.http_routes"],
        inject=inject,
    )
    # SessionRuntime was built using fallback DurableObject and _WsHelperMixin
    assert issubclass(mod.SessionRuntime, DurableObject)
    assert issubclass(mod.SessionRuntime, _WsHelperMixin)
