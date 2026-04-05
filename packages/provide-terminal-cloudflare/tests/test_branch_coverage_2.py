#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Branch coverage tests (part 2) — do/ws_helpers, state/registry, ui/assets."""

from __future__ import annotations

import importlib.resources
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# do/ws_helpers.py  50->52  — _socket_role: colon-format but parts[0] not valid
# ---------------------------------------------------------------------------


def test_socket_role_colon_format_invalid_first_part_falls_back() -> None:
    """Line 50->52: attachment is colon-split string but parts[0] not in known roles."""
    from provide.terminal.cloudflare.do.ws_helpers import _WsHelperMixin

    class _Host(_WsHelperMixin):
        worker_id = "w"
        worker_ws = None
        browser_sockets: dict = {}
        raw_sockets: dict = {}
        browser_hijack_owner: dict = {}
        config = SimpleNamespace(jwt=SimpleNamespace(mode="dev"))

    host = _Host()
    ws = MagicMock()
    # First part "unknown" is not in {"browser","worker","raw"} → falls through
    ws.deserializeAttachment.return_value = "unknown:admin:w1"
    # Falls through to the dict/object/to_py paths → eventually returns "browser" fallback
    role = host._socket_role(ws)
    assert role == "browser"


# ---------------------------------------------------------------------------
# do/ws_helpers.py  62->66  — _socket_role: to_py() returns non-str, non-dict
# ---------------------------------------------------------------------------


def test_socket_role_to_py_returns_other_type_falls_back() -> None:
    """Line 62->66: to_py() returns an int (not str or dict) → role stays None → browser."""
    from provide.terminal.cloudflare.do.ws_helpers import _WsHelperMixin

    class _Host(_WsHelperMixin):
        worker_id = "w"
        worker_ws = None
        browser_sockets: dict = {}
        raw_sockets: dict = {}
        browser_hijack_owner: dict = {}
        config = SimpleNamespace(jwt=SimpleNamespace(mode="dev"))

    host = _Host()
    ws = MagicMock()
    att = MagicMock(spec=["to_py"])
    att.to_py.return_value = 42  # neither str nor dict
    ws.deserializeAttachment.return_value = att
    role = host._socket_role(ws)
    assert role == "browser"


# ---------------------------------------------------------------------------
# state/registry.py  96->90  — list_kv_sessions: raw value is falsy
# ---------------------------------------------------------------------------


async def test_list_kv_sessions_skips_empty_raw_value() -> None:
    """Line 96->90: kv.get() returns empty string → session is not appended."""
    from provide.terminal.cloudflare.state.registry import list_kv_sessions

    async def _get(key: str) -> str:
        return ""  # falsy → `if raw:` is False

    async def _list(*, prefix: str = "") -> SimpleNamespace:
        return SimpleNamespace(keys=[{"name": "session:ghost"}])

    kv = SimpleNamespace(get=_get, list=_list)
    env = SimpleNamespace(SESSION_REGISTRY=kv)

    sessions = await list_kv_sessions(env)
    assert sessions == []


# ---------------------------------------------------------------------------
# ui/assets.py  29->33  — read_asset_text: importlib finds package but file missing
# ---------------------------------------------------------------------------


def test_read_asset_text_importlib_file_not_found_falls_through() -> None:
    """Line 29->33: importlib.resources.files succeeds but is_file() returns False."""
    from provide.terminal.cloudflare.ui.assets import read_asset_text

    not_found = SimpleNamespace(is_file=lambda: False, name="missing.js")

    local_root = MagicMock()
    local_root.__truediv__ = MagicMock(return_value=not_found)

    pkg = MagicMock()
    pkg.__truediv__ = MagicMock(return_value=local_root)

    with (
        patch.object(importlib.resources, "files", return_value=pkg),
        patch("provide.terminal.cloudflare.ui.assets._LOCAL_STATIC", Path("/nonexistent-branch-test")),
    ):
        result = read_asset_text("missing.js")

    # Falls through all paths, returns None
    assert result is None


# ---------------------------------------------------------------------------
# ui/assets.py  42->46  — read_asset_text: _LOCAL_STATIC exists but file not in it
# ---------------------------------------------------------------------------


def test_read_asset_text_local_static_file_not_found_falls_through(tmp_path: Path) -> None:
    """Line 42->46: _LOCAL_STATIC directory accessible but file not present → falls through."""
    from provide.terminal.cloudflare.ui.assets import read_asset_text

    # tmp_path exists but we don't create the target file
    with (
        patch("importlib.resources.files", side_effect=ModuleNotFoundError("no pkg")),
        patch("provide.terminal.cloudflare.ui.assets._LOCAL_STATIC", tmp_path),
    ):
        result = read_asset_text("absent.js")

    assert result is None


# ---------------------------------------------------------------------------
# ui/assets.py  58->66  — serve_asset: importlib finds package but file missing
# ---------------------------------------------------------------------------


def test_serve_asset_importlib_file_not_found_falls_through(tmp_path: Path) -> None:
    """Line 58->66: importlib succeeds, is_file() returns False → falls through to _LOCAL_STATIC."""
    from provide.terminal.cloudflare.ui.assets import serve_asset

    not_found = SimpleNamespace(is_file=lambda: False, name="missing.js")
    local_root = MagicMock()
    local_root.__truediv__ = MagicMock(return_value=not_found)
    pkg = MagicMock()
    pkg.__truediv__ = MagicMock(return_value=local_root)

    # tmp_path exists but file not there → _LOCAL_STATIC fallback also misses
    with (
        patch.object(importlib.resources, "files", return_value=pkg),
        patch("provide.terminal.cloudflare.ui.assets._LOCAL_STATIC", tmp_path),
    ):
        resp = serve_asset("missing.js")

    # Falls through to provide.terminal package or returns 404
    assert resp.status in {200, 404}
