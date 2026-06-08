#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the vanilla (web-component) Vite manifest helpers in ui.py.

Covers the branches the vite-migration added to ``_read_vanilla_manifest`` and
``_resolve_vanilla_asset`` (the ``.vite/`` subdir hit, the no-manifest path, the
read-error path, and the basename fallback) that page-rendering tests don't
otherwise exercise.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from provide.uterm.server import ui


@pytest.fixture(autouse=True)
def _reset_vanilla_cache():
    ui._vanilla_manifest = None
    ui._vanilla_manifest_loaded = False
    yield
    ui._vanilla_manifest = None
    ui._vanilla_manifest_loaded = False


class _FakePath:
    """Minimal importlib.resources Traversable stub keyed by joined path parts."""

    def __init__(self, parts: list[str], files: dict[str, str]) -> None:
        self._parts = parts
        self._files = files  # "joined/path" -> contents; presence == is_file()

    def __truediv__(self, name: str) -> _FakePath:
        return _FakePath([*self._parts, name], self._files)

    def is_file(self) -> bool:
        return "/".join(self._parts) in self._files

    def read_text(self, encoding: str = "utf-8") -> str:
        return self._files["/".join(self._parts)]


_VITE_KEY = "frontend/.vite/vanilla-manifest.json"


class TestReadVanillaManifest:
    def test_loads_from_vite_subdir(self) -> None:
        """``.vite/vanilla-manifest.json`` present → loaded without the root fallback (37->40)."""
        data = {"src/hijack.ts": {"file": "assets/hijack-abc.js"}}
        with mock.patch("importlib.resources.files", return_value=_FakePath([], {_VITE_KEY: json.dumps(data)})):
            result = ui._read_vanilla_manifest()
        assert result is not None
        assert "src/hijack.ts" in result

    def test_returns_none_when_neither_manifest_present(self) -> None:
        """Neither ``.vite/`` nor root manifest present → None (40->46)."""
        with mock.patch("importlib.resources.files", return_value=_FakePath([], {})):
            result = ui._read_vanilla_manifest()
        assert result is None

    def test_handles_read_error_gracefully(self) -> None:
        """A failure while resolving/reading the manifest is swallowed → None (44-45)."""
        with mock.patch("importlib.resources.files", side_effect=Exception("boom")):
            result = ui._read_vanilla_manifest()
        assert result is None


class TestResolveVanillaAsset:
    def test_falls_back_to_basename_when_no_manifest(self) -> None:
        """No manifest → derive the served filename from the entry name (line 56)."""
        ui._vanilla_manifest = None
        ui._vanilla_manifest_loaded = True
        assert ui._resolve_vanilla_asset("src/hijack.ts") == "hijack.js"

    def test_resolves_hashed_file_from_manifest(self) -> None:
        """Manifest present → return the hashed file path verbatim."""
        ui._vanilla_manifest = {"src/hijack.ts": {"file": "assets/hijack-abc.js"}}
        ui._vanilla_manifest_loaded = True
        assert ui._resolve_vanilla_asset("src/hijack.ts") == "assets/hijack-abc.js"

    def test_falls_back_when_entry_lacks_file(self) -> None:
        """Entry present but malformed (no ``file`` key) → basename fallback (53->56)."""
        ui._vanilla_manifest = {"src/hijack.ts": {"css": ["x.css"]}}
        ui._vanilla_manifest_loaded = True
        assert ui._resolve_vanilla_asset("src/hijack.ts") == "hijack.js"
