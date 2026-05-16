#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Unit tests for ui/assets.py — serve_asset()."""

from __future__ import annotations

import importlib.resources
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_file(name: str, content: str) -> SimpleNamespace:
    """Traversable-like file object that always reports is_file=True."""
    return SimpleNamespace(
        is_file=lambda: True,
        name=name,
        read_text=lambda encoding="utf-8": content,
    )


def _not_found_file() -> SimpleNamespace:
    """Traversable-like object that reports is_file=False."""
    return SimpleNamespace(is_file=lambda: False, name="")


def _patch_files_local(file_obj):
    """Patch importlib.resources.files so files(...) / 'static' / rel → file_obj."""
    local_root = MagicMock()
    local_root.__truediv__.return_value = file_obj  # local_root / rel

    pkg = MagicMock()
    pkg.__truediv__.return_value = local_root  # pkg / "static"

    return patch.object(importlib.resources, "files", return_value=pkg)


def _patch_files_fallthrough(file_obj):
    """First call (local pkg) raises ModuleNotFoundError; second returns file_obj."""
    call_count = [0]

    def _side_effect(pkg_name: str):
        call_count[0] += 1
        if call_count[0] == 1:
            raise ModuleNotFoundError("no local static")
        # Second call: provide.uterm frontend
        frontend_dir = MagicMock()
        frontend_dir.__truediv__.return_value = file_obj  # frontend_dir / rel
        root = MagicMock()
        root.__truediv__.return_value = frontend_dir  # root / "frontend"
        return root

    return patch.object(importlib.resources, "files", side_effect=_side_effect)


# ---------------------------------------------------------------------------
# Path traversal protection
# ---------------------------------------------------------------------------


def test_path_traversal_single_dotdot_blocked() -> None:
    from provide.uterm.cloudflare.ui.assets import serve_asset

    resp = serve_asset("../../etc/passwd")
    assert resp.status == 403


def test_path_traversal_nested_blocked() -> None:
    from provide.uterm.cloudflare.ui.assets import serve_asset

    resp = serve_asset("a/../../../secret")
    assert resp.status == 403


# ---------------------------------------------------------------------------
# Local static file served
# ---------------------------------------------------------------------------


def test_serve_js_file_local() -> None:
    from provide.uterm.cloudflare.ui.assets import serve_asset

    with _patch_files_local(_mock_file("app.js", "console.log('hi');")):
        resp = serve_asset("app.js")

    assert resp.status == 200
    assert "javascript" in resp.headers["content-type"]
    assert "console.log" in resp.body


def test_serve_css_file_local() -> None:
    from provide.uterm.cloudflare.ui.assets import serve_asset

    with _patch_files_local(_mock_file("style.css", "body { color: red; }")):
        resp = serve_asset("style.css")

    assert resp.status == 200
    assert "css" in resp.headers["content-type"]


def test_serve_html_file_local() -> None:
    from provide.uterm.cloudflare.ui.assets import serve_asset

    with _patch_files_local(_mock_file("index.html", "<html/>")):
        resp = serve_asset("index.html")

    assert resp.status == 200
    assert "html" in resp.headers["content-type"]


def test_serve_unknown_mime_type() -> None:
    from provide.uterm.cloudflare.ui.assets import serve_asset

    with _patch_files_local(_mock_file("data.bin", "raw bytes")):
        resp = serve_asset("data.bin")

    assert resp.status == 200
    assert resp.headers["content-type"] == "application/octet-stream"


# ---------------------------------------------------------------------------
# Fallback: local static missing → provide.uterm frontend
# ---------------------------------------------------------------------------


def test_falls_through_to_frontend_package() -> None:
    from provide.uterm.cloudflare.ui.assets import serve_asset

    # Patch _LOCAL_STATIC to an empty dir so the filesystem fallback (option 2)
    # doesn't intercept when ui/static/ is populated by the wrangler build.
    with (
        _patch_files_fallthrough(_mock_file("hijack.js", "// hijack")),
        patch("provide.uterm.cloudflare.ui.assets._LOCAL_STATIC", Path("/nonexistent-test-dir")),
    ):
        resp = serve_asset("hijack.js")

    assert resp.status == 200
    assert "javascript" in resp.headers["content-type"]
    assert "hijack" in resp.body


def test_frontend_file_not_found_returns_404() -> None:
    from provide.uterm.cloudflare.ui.assets import serve_asset

    with (
        _patch_files_fallthrough(_not_found_file()),
        patch("provide.uterm.cloudflare.ui.assets._LOCAL_STATIC", Path("/nonexistent-test-dir")),
    ):
        resp = serve_asset("nonexistent.js")

    assert resp.status == 404
    assert "not found" in resp.body


def test_frontend_module_not_found_returns_404() -> None:
    from provide.uterm.cloudflare.ui.assets import serve_asset

    call_count = [0]

    def _always_raise(pkg_name: str):
        call_count[0] += 1
        raise ModuleNotFoundError(f"no package ({call_count[0]})")

    with patch.object(importlib.resources, "files", side_effect=_always_raise):
        resp = serve_asset("missing.js")

    assert resp.status == 404
    assert "unavailable" in resp.body


# ---------------------------------------------------------------------------
# read_asset_text
# ---------------------------------------------------------------------------


def test_read_asset_text_path_traversal_returns_none() -> None:
    from provide.uterm.cloudflare.ui.assets import read_asset_text

    assert read_asset_text("../../etc/passwd") is None


def test_read_asset_text_local_found() -> None:
    from provide.uterm.cloudflare.ui.assets import read_asset_text

    with _patch_files_local(_mock_file("terminal.html", "<html>terminal</html>")):
        result = read_asset_text("terminal.html")

    assert result == "<html>terminal</html>"


def test_read_asset_text_file_relative_found(tmp_path) -> None:
    from provide.uterm.cloudflare.ui.assets import read_asset_text

    fake = tmp_path / "terminal.html"
    fake.write_text("<html>local</html>")

    def _raise_first(pkg_name: str):
        raise ModuleNotFoundError("no local static")

    with (
        patch.object(importlib.resources, "files", side_effect=_raise_first),
        patch("provide.uterm.cloudflare.ui.assets._LOCAL_STATIC", tmp_path),
    ):
        result = read_asset_text("terminal.html")

    assert result == "<html>local</html>"


def test_read_asset_text_frontend_fallback_found() -> None:
    from provide.uterm.cloudflare.ui.assets import read_asset_text

    with (
        _patch_files_fallthrough(_mock_file("terminal.html", "<html>frontend</html>")),
        patch("provide.uterm.cloudflare.ui.assets._LOCAL_STATIC", Path("/nonexistent-test-dir")),
    ):
        result = read_asset_text("terminal.html")

    assert result == "<html>frontend</html>"


def test_read_asset_text_local_static_oserror_falls_through() -> None:
    from provide.uterm.cloudflare.ui.assets import read_asset_text

    class _BrokenPath:
        def __truediv__(self, other):
            return self

        def is_file(self):
            raise OSError("permission denied")

    def _raise_all(pkg_name: str):
        raise ModuleNotFoundError("no pkg")

    with (
        patch.object(importlib.resources, "files", side_effect=_raise_all),
        patch("provide.uterm.cloudflare.ui.assets._LOCAL_STATIC", _BrokenPath()),
    ):
        result = read_asset_text("terminal.html")

    assert result is None


def test_read_asset_text_not_found_returns_none() -> None:
    from provide.uterm.cloudflare.ui.assets import read_asset_text

    call_count = [0]

    def _always_raise(pkg_name: str):
        call_count[0] += 1
        raise ModuleNotFoundError(f"no package ({call_count[0]})")

    with (
        patch.object(importlib.resources, "files", side_effect=_always_raise),
        patch("provide.uterm.cloudflare.ui.assets._LOCAL_STATIC", Path("/nonexistent-test-dir")),
    ):
        result = read_asset_text("terminal.html")

    assert result is None
