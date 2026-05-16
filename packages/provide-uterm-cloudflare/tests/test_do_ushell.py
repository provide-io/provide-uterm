#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.cloudflare.do.ushell — CF ushell adapter."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


def _make_runtime(
    worker_id="ushell-abc",
    input_mode="open",
    ushell=None,
    ushell_started=False,
    storage=None,
):
    """Build a minimal mock runtime object."""
    ctx = SimpleNamespace(storage=storage)
    rt = MagicMock()
    rt.worker_id = worker_id
    rt.input_mode = input_mode
    rt._ushell = ushell
    rt._ushell_started = ushell_started
    rt.env = SimpleNamespace(SESSION_REGISTRY=None)
    rt.ctx = ctx
    rt.broadcast_to_browsers = AsyncMock()
    rt.broadcast_worker_frame = AsyncMock()
    return rt


# ---------------------------------------------------------------------------
# _load_connector
# ---------------------------------------------------------------------------


def test_load_connector_import_error():
    """When UshellConnector cannot be imported, returns None."""
    from provide.uterm.cloudflare.do import ushell as ushell_mod

    # Block both shell modules so the ImportError branch is exercised.
    orig_connector = sys.modules.get("provide.uterm.shell.terminal._connector")
    orig_commands = sys.modules.get("provide.uterm.shell._commands")
    sys.modules["provide.uterm.shell.terminal._connector"] = None  # type: ignore[assignment]
    sys.modules["provide.uterm.shell._commands"] = None  # type: ignore[assignment]
    # Reset module-level sentinel so the branch runs fresh.
    prev_error = ushell_mod._IMPORT_ERROR
    ushell_mod._IMPORT_ERROR = None
    try:
        result = ushell_mod._load_connector("ushell-x", SimpleNamespace(), storage=None)
        assert result is None
        assert ushell_mod._IMPORT_ERROR is not None
    finally:
        ushell_mod._IMPORT_ERROR = prev_error
        if orig_connector is None:
            sys.modules.pop("provide.uterm.shell.terminal._connector", None)
        else:
            sys.modules["provide.uterm.shell.terminal._connector"] = orig_connector
        if orig_commands is None:
            sys.modules.pop("provide.uterm.shell._commands", None)
        else:
            sys.modules["provide.uterm.shell._commands"] = orig_commands


def test_load_connector_success_with_list_kv_sessions():
    """Happy path: UshellConnector created with list_kv_sessions in ctx."""
    from provide.uterm.cloudflare.do import ushell as ushell_mod

    env = SimpleNamespace(SESSION_REGISTRY=MagicMock())
    connector = ushell_mod._load_connector("ushell-123", env, storage=None)
    assert connector is not None
    assert "list_kv_sessions" in connector._dispatcher._ctx


def test_load_connector_with_storage():
    """Storage is wired into ctx when provided."""
    from provide.uterm.cloudflare.do import ushell as ushell_mod

    storage = MagicMock()
    env = SimpleNamespace()
    connector = ushell_mod._load_connector("ushell-s", env, storage=storage)
    assert connector is not None
    assert connector._dispatcher._ctx.get("storage") is storage


def test_load_connector_without_storage():
    """Storage key absent from ctx when storage=None."""
    from provide.uterm.cloudflare.do import ushell as ushell_mod

    env = SimpleNamespace()
    connector = ushell_mod._load_connector("ushell-s", env, storage=None)
    assert connector is not None
    assert "storage" not in connector._dispatcher._ctx


async def test_load_connector_primary_list_kv_sessions_callable():
    """The _list_sessions closure actually calls list_kv_sessions(env)."""
    from provide.uterm.cloudflare.do import ushell as ushell_mod

    fake_lks = AsyncMock(return_value=[{"id": "s1"}])
    env = SimpleNamespace(SESSION_REGISTRY=MagicMock())

    with patch("provide.uterm.cloudflare.state.registry.list_kv_sessions", fake_lks):
        connector = ushell_mod._load_connector("ushell-call", env, storage=None)
        assert connector is not None
        fn = connector._dispatcher._ctx["list_kv_sessions"]
        result = await fn()
    assert result == [{"id": "s1"}]
    fake_lks.assert_called_once_with(env)


def test_load_connector_fallback_import_path():
    """Falls back to state.registry when primary import fails."""
    from provide.uterm.cloudflare.do import ushell as ushell_mod

    # Block the primary import path.
    fake_lks = AsyncMock(return_value=[])
    fake_state_registry = MagicMock()
    fake_state_registry.list_kv_sessions = fake_lks

    orig = sys.modules.get("provide.uterm.cloudflare.state.registry")
    sys.modules["provide.uterm.cloudflare.state.registry"] = None  # type: ignore[assignment]
    sys.modules["state.registry"] = fake_state_registry
    try:
        env = SimpleNamespace()
        connector = ushell_mod._load_connector("ushell-fb", env, storage=None)
        assert connector is not None
        assert "list_kv_sessions" in connector._dispatcher._ctx
    finally:
        if orig is None:
            sys.modules.pop("provide.uterm.cloudflare.state.registry", None)
        else:
            sys.modules["provide.uterm.cloudflare.state.registry"] = orig
        sys.modules.pop("state.registry", None)


async def test_load_connector_fallback_list_kv_sessions_callable():
    """The fallback _list_sessions2 closure actually calls _lks(env)."""
    from provide.uterm.cloudflare.do import ushell as ushell_mod

    fake_lks = AsyncMock(return_value=[{"id": "s2"}])
    fake_state_registry = MagicMock()
    fake_state_registry.list_kv_sessions = fake_lks

    orig = sys.modules.get("provide.uterm.cloudflare.state.registry")
    sys.modules["provide.uterm.cloudflare.state.registry"] = None  # type: ignore[assignment]
    sys.modules["state.registry"] = fake_state_registry
    try:
        env = SimpleNamespace()
        connector = ushell_mod._load_connector("ushell-fbc", env, storage=None)
        assert connector is not None
        fn = connector._dispatcher._ctx["list_kv_sessions"]
        result = await fn()
    finally:
        if orig is None:
            sys.modules.pop("provide.uterm.cloudflare.state.registry", None)
        else:
            sys.modules["provide.uterm.cloudflare.state.registry"] = orig
        sys.modules.pop("state.registry", None)

    assert result == [{"id": "s2"}]
    fake_lks.assert_called_once_with(env)


def test_load_connector_both_imports_fail():
    """No list_kv_sessions in ctx when both import paths fail."""
    from provide.uterm.cloudflare.do import ushell as ushell_mod

    orig_primary = sys.modules.get("provide.uterm.cloudflare.state.registry")
    orig_fallback = sys.modules.get("state.registry")
    sys.modules["provide.uterm.cloudflare.state.registry"] = None  # type: ignore[assignment]
    sys.modules["state.registry"] = None  # type: ignore[assignment]
    try:
        env = SimpleNamespace()
        connector = ushell_mod._load_connector("ushell-nolks", env, storage=None)
        assert connector is not None
        assert "list_kv_sessions" not in connector._dispatcher._ctx
    finally:
        if orig_primary is None:
            sys.modules.pop("provide.uterm.cloudflare.state.registry", None)
        else:
            sys.modules["provide.uterm.cloudflare.state.registry"] = orig_primary
        if orig_fallback is None:
            sys.modules.pop("state.registry", None)
        else:
            sys.modules["state.registry"] = orig_fallback


# ---------------------------------------------------------------------------
# init_ushell
# ---------------------------------------------------------------------------


def test_init_ushell_non_ushell_session():
    """Non-ushell session IDs are ignored."""
    from provide.uterm.cloudflare.do.ushell import init_ushell

    rt = _make_runtime(worker_id="shell-xyz")
    init_ushell(rt)
    assert rt._ushell is None


def test_init_ushell_already_initialized():
    """No-op if _ushell is already set."""
    from provide.uterm.cloudflare.do.ushell import init_ushell

    existing = MagicMock()
    rt = _make_runtime(ushell=existing)
    init_ushell(rt)
    assert rt._ushell is existing


def test_init_ushell_connector_none():
    """Logs error when _load_connector returns None."""
    from provide.uterm.cloudflare.do import ushell as ushell_mod

    rt = _make_runtime()
    with patch.object(ushell_mod, "_load_connector", return_value=None):
        ushell_mod.init_ushell(rt)
    assert rt._ushell is None


def test_init_ushell_happy_path():
    """Sets input_mode=open and attaches connector."""
    from provide.uterm.cloudflare.do import ushell as ushell_mod

    rt = _make_runtime()
    fake_connector = MagicMock()
    with patch.object(ushell_mod, "_load_connector", return_value=fake_connector):
        ushell_mod.init_ushell(rt)
    assert rt.input_mode == "open"
    assert rt._ushell is fake_connector


def test_init_ushell_passes_storage():
    """_load_connector is called with storage from ctx."""
    from provide.uterm.cloudflare.do import ushell as ushell_mod

    storage = MagicMock()
    rt = _make_runtime(storage=storage)
    fake_connector = MagicMock()
    with patch.object(ushell_mod, "_load_connector", return_value=fake_connector) as mock_load:
        ushell_mod.init_ushell(rt)
    mock_load.assert_called_once_with(rt.worker_id, rt.env, storage=storage)


def test_init_ushell_no_ctx_attribute():
    """Works even when runtime has no ctx attribute (storage=None fallback)."""
    from provide.uterm.cloudflare.do import ushell as ushell_mod

    # Use SimpleNamespace so missing attributes don't auto-create (unlike MagicMock)
    rt = SimpleNamespace(
        worker_id="ushell-test",
        input_mode="open",
        _ushell=None,
        _ushell_started=False,
        env=SimpleNamespace(),
        # NOTE: no 'ctx' attribute — tests getattr(..., None) fallback
    )
    fake_connector = MagicMock()
    with patch.object(ushell_mod, "_load_connector", return_value=fake_connector) as mock_load:
        ushell_mod.init_ushell(rt)
    mock_load.assert_called_once_with(rt.worker_id, rt.env, storage=None)


def test_init_ushell_ctx_no_storage_attr():
    """Works when ctx exists but has no storage attribute (storage=None fallback)."""
    from provide.uterm.cloudflare.do import ushell as ushell_mod

    rt = _make_runtime()
    rt.ctx = SimpleNamespace()  # ctx without storage attr
    fake_connector = MagicMock()
    with patch.object(ushell_mod, "_load_connector", return_value=fake_connector) as mock_load:
        ushell_mod.init_ushell(rt)
    mock_load.assert_called_once_with(rt.worker_id, rt.env, storage=None)


# ---------------------------------------------------------------------------
# on_browser_connected
# ---------------------------------------------------------------------------


async def test_on_browser_connected_no_ushell():
    """No-op when _ushell is None."""
    from provide.uterm.cloudflare.do.ushell import on_browser_connected

    rt = _make_runtime(ushell=None)
    await on_browser_connected(rt)
    rt.broadcast_worker_frame.assert_not_called()


async def test_on_browser_connected_already_started():
    """No-op when _ushell_started is True."""
    from provide.uterm.cloudflare.do.ushell import on_browser_connected

    ushell = MagicMock()
    ushell.start = AsyncMock()
    rt = _make_runtime(ushell=ushell, ushell_started=True)
    await on_browser_connected(rt)
    ushell.start.assert_not_called()


async def test_on_browser_connected_happy_path_sends_frames():
    """Broadcasts worker_connected and term frames, skips worker_hello."""
    from provide.uterm.cloudflare.do.ushell import on_browser_connected

    ushell = MagicMock()
    ushell.start = AsyncMock()
    ushell.poll_messages = AsyncMock(
        return_value=[
            {"type": "worker_hello", "input_mode": "open"},
            {"type": "term", "data": "banner"},
        ]
    )
    rt = _make_runtime(ushell=ushell, ushell_started=False)

    await on_browser_connected(rt)

    assert rt._ushell_started is True
    rt.broadcast_worker_frame.assert_called_once()
    # Only the "term" frame should be broadcast (worker_hello is skipped)
    rt.broadcast_to_browsers.assert_called_once_with({"type": "term", "data": "banner"})


async def test_on_browser_connected_all_frames_worker_hello():
    """No broadcast_to_browsers calls when all frames are worker_hello."""
    from provide.uterm.cloudflare.do.ushell import on_browser_connected

    ushell = MagicMock()
    ushell.start = AsyncMock()
    ushell.poll_messages = AsyncMock(
        return_value=[
            {"type": "worker_hello", "input_mode": "open"},
        ]
    )
    rt = _make_runtime(ushell=ushell, ushell_started=False)

    await on_browser_connected(rt)

    assert rt._ushell_started is True
    rt.broadcast_worker_frame.assert_called_once()
    rt.broadcast_to_browsers.assert_not_called()
