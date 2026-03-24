#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for Vite manifest integration in ui.py."""

from __future__ import annotations

import json
from unittest import mock

import pytest

from provide.terminal.server import ui


@pytest.fixture(autouse=True)
def _reset_manifest_cache():
    """Reset the module-level manifest cache before each test."""
    ui._vite_manifest = None
    ui._vite_manifest_loaded = False
    yield
    ui._vite_manifest = None
    ui._vite_manifest_loaded = False


class TestReadViteManifest:
    def test_returns_none_when_no_manifest(self):
        fake_path = mock.MagicMock()
        fake_path.is_file.return_value = False
        with mock.patch("importlib.resources.files") as mock_files:
            mock_files.return_value.__truediv__ = lambda self, name: (
                fake_path if name == "frontend" else mock.MagicMock(__truediv__=lambda s, n: fake_path)
            )
            # Simulate the path chain: files("provide.terminal") / "frontend" / ".vite" / "manifest.json"
            frontend_mock = mock.MagicMock()
            vite_mock = mock.MagicMock()
            manifest_mock = mock.MagicMock()
            manifest_mock.is_file.return_value = False
            vite_mock.__truediv__ = lambda self, name: manifest_mock
            frontend_mock.__truediv__ = lambda self, name: vite_mock
            mock_files.return_value = mock.MagicMock(__truediv__=lambda self, name: frontend_mock)

            result = ui._read_vite_manifest()
        assert result is None

    def test_returns_parsed_manifest_when_present(self):
        manifest_data = {
            "src/main.tsx": {
                "file": "assets/main-abc123.js",
                "css": ["assets/main-def456.css"],
            }
        }
        manifest_mock = mock.MagicMock()
        manifest_mock.is_file.return_value = True
        manifest_mock.read_text.return_value = json.dumps(manifest_data)
        frontend_mock = mock.MagicMock()
        vite_mock = mock.MagicMock()
        vite_mock.__truediv__ = lambda self, name: manifest_mock
        frontend_mock.__truediv__ = lambda self, name: vite_mock

        with mock.patch("importlib.resources.files") as mock_files:
            mock_files.return_value = mock.MagicMock(__truediv__=lambda self, name: frontend_mock)
            result = ui._read_vite_manifest()

        assert result is not None
        assert "src/main.tsx" in result

    def test_caches_result(self):
        ui._vite_manifest = {"cached": True}
        ui._vite_manifest_loaded = True
        result = ui._read_vite_manifest()
        assert result == {"cached": True}

    def test_handles_read_error_gracefully(self):
        with mock.patch("importlib.resources.files", side_effect=Exception("boom")):
            result = ui._read_vite_manifest()
        assert result is None


class TestViteEntryTags:
    def test_empty_when_no_manifest(self):
        ui._vite_manifest = None
        ui._vite_manifest_loaded = True
        assert ui._vite_entry_tags("/assets") == ""

    def test_empty_when_entry_missing(self):
        ui._vite_manifest = {"other.tsx": {"file": "other.js"}}
        ui._vite_manifest_loaded = True
        assert ui._vite_entry_tags("/assets") == ""

    def test_generates_script_and_css_tags(self):
        ui._vite_manifest = {
            "src/main.tsx": {
                "file": "assets/main-abc123.js",
                "css": ["assets/main-def456.css"],
            }
        }
        ui._vite_manifest_loaded = True
        tags = ui._vite_entry_tags("/assets")
        assert "assets/main-def456.css" in tags
        assert "assets/main-abc123.js" in tags
        assert "type='module'" in tags

    def test_no_css_key(self):
        ui._vite_manifest = {"src/main.tsx": {"file": "assets/main-abc123.js"}}
        ui._vite_manifest_loaded = True
        tags = ui._vite_entry_tags("/assets")
        assert "link" not in tags
        assert "assets/main-abc123.js" in tags

    def test_no_file_key(self):
        ui._vite_manifest = {"src/main.tsx": {"css": ["assets/main.css"]}}
        ui._vite_manifest_loaded = True
        tags = ui._vite_entry_tags("/assets")
        assert "assets/main.css" in tags
        assert "script" not in tags


class TestShellViteIntegration:
    def test_dashboard_uses_vite_when_manifest_present(self):
        ui._vite_manifest = {
            "src/main.tsx": {
                "file": "assets/main-abc.js",
                "css": ["assets/main-xyz.css"],
            }
        }
        ui._vite_manifest_loaded = True
        html = ui.operator_dashboard_html("Test", "/app", "/assets")
        assert "assets/main-abc.js" in html
        assert "assets/main-xyz.css" in html
        # Legacy vanilla entry should NOT be present
        assert "server-session-page.js" not in html
        assert "server-app-foundation.css" not in html

    def test_dashboard_uses_legacy_when_no_manifest(self):
        ui._vite_manifest = None
        ui._vite_manifest_loaded = True
        html = ui.operator_dashboard_html("Test", "/app", "/assets")
        assert "server-session-page.js" in html
        assert "server-app-foundation.css" in html

    def test_session_page_includes_hijack_js_always(self):
        ui._vite_manifest = {"src/main.tsx": {"file": "assets/main-abc.js", "css": []}}
        ui._vite_manifest_loaded = True
        html = ui.session_page_html("Test", "/assets", "sess-1", operator=True, app_path="/app")
        # hijack.js always loaded for session/operator pages (vanilla widget)
        assert "hijack.js" in html
        assert "assets/main-abc.js" in html

    def test_replay_uses_vite(self):
        ui._vite_manifest = {"src/main.tsx": {"file": "assets/main-abc.js", "css": []}}
        ui._vite_manifest_loaded = True
        html = ui.replay_page_html("Test", "/assets", "sess-1", app_path="/app")
        assert "assets/main-abc.js" in html
        assert "server-replay-page.js" not in html

    def test_connect_uses_vite(self):
        ui._vite_manifest = {"src/main.tsx": {"file": "assets/main-abc.js", "css": []}}
        ui._vite_manifest_loaded = True
        html = ui.connect_page_html("Test", "/assets", "/app")
        assert "assets/main-abc.js" in html
        assert "server-session-page.js" not in html
