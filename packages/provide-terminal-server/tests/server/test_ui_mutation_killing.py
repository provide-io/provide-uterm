#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for server/ui.py."""

from __future__ import annotations

import json
from unittest import mock

import pytest

from provide.terminal.server import ui
from provide.terminal.server.ui import (
    _bootstrap_tag,
    _shell,
    _vite_entry_tags,
    operator_dashboard_html,
)


@pytest.fixture(autouse=True)
def _reset_vite_cache():
    """Reset vite manifest cache before and after each test."""
    ui._vite_manifest = None
    ui._vite_manifest_loaded = False
    yield
    ui._vite_manifest = None
    ui._vite_manifest_loaded = False


# ---------------------------------------------------------------------------
# _read_vite_manifest mutation killers
# mutmut_1: _vite_manifest_loaded = None  (not True — would re-read every call)
# mutmut_2: _vite_manifest_loaded = False (not True — infinite re-read)
# mutmut_7: files(None)  (wrong package name)
# mutmut_8: files("XXprovide.terminalXX")  (wrong package name)
# mutmut_9-15: path components wrong
# mutmut_17-19: is_file() check broken
# mutmut_22-36: manifest loading/encoding broken
# ---------------------------------------------------------------------------


class TestReadViteManifestMutationKilling:
    def test_sets_loaded_flag_to_true(self):
        """After calling _read_vite_manifest, _vite_manifest_loaded must be True.
        Kills mutmut_1 (=None) and mutmut_2 (=False)."""
        with mock.patch("importlib.resources.files") as mock_files:
            manifest_mock = mock.MagicMock()
            manifest_mock.is_file.return_value = False
            vite_mock = mock.MagicMock()
            vite_mock.__truediv__ = lambda self, n: manifest_mock
            frontend_mock = mock.MagicMock()
            frontend_mock.__truediv__ = lambda self, n: vite_mock
            mock_files.return_value = mock.MagicMock(__truediv__=lambda self, n: frontend_mock)
            ui._read_vite_manifest()
        assert ui._vite_manifest_loaded is True

    def test_second_call_uses_cache_no_reimport(self):
        """Once loaded, second call returns cached value without re-reading.
        Kills mutmut_1 (None is falsy → cache miss) and mutmut_2 (False → cache miss)."""
        ui._vite_manifest = {"cached": "yes"}
        ui._vite_manifest_loaded = True
        call_count = [0]

        with mock.patch("importlib.resources.files") as mock_files:
            mock_files.side_effect = lambda x: call_count.__setitem__(0, call_count[0] + 1) or mock.MagicMock()
            result = ui._read_vite_manifest()

        # importlib.resources.files should NOT have been called (cache hit)
        assert call_count[0] == 0
        assert result == {"cached": "yes"}

    def test_uses_correct_package_name(self):
        """importlib.resources.files called with 'provide.terminal.server'.
        Kills mutmut_7 (None) and mutmut_8 ('XXprovide.terminalXX')."""
        captured_args = []

        def mock_files(pkg):
            captured_args.append(pkg)
            m = mock.MagicMock()
            manifest = mock.MagicMock()
            manifest.is_file.return_value = False
            vite = mock.MagicMock()
            vite.__truediv__ = lambda self, n: manifest
            frontend = mock.MagicMock()
            frontend.__truediv__ = lambda self, n: vite
            m.__truediv__ = lambda self, n: frontend
            return m

        with mock.patch("importlib.resources.files", side_effect=mock_files):
            ui._read_vite_manifest()

        assert len(captured_args) == 1
        assert captured_args[0] == "provide.terminal.server"

    def test_manifest_path_reads_utf8(self):
        """manifest.read_text called with encoding='utf-8'.
        Kills mutmut variants that change encoding."""
        manifest_data = {"src/main.tsx": {"file": "main.js", "css": []}}
        captured_kwargs = []

        def mock_read_text(**kwargs):
            captured_kwargs.append(kwargs)
            return json.dumps(manifest_data)

        manifest_mock = mock.MagicMock()
        manifest_mock.is_file.return_value = True
        manifest_mock.read_text = mock_read_text
        vite_mock = mock.MagicMock()
        vite_mock.__truediv__ = lambda self, n: manifest_mock
        frontend_mock = mock.MagicMock()
        frontend_mock.__truediv__ = lambda self, n: vite_mock

        with mock.patch("importlib.resources.files") as mf:
            mf.return_value = mock.MagicMock(__truediv__=lambda self, n: frontend_mock)
            result = ui._read_vite_manifest()

        assert len(captured_kwargs) == 1
        assert captured_kwargs[0].get("encoding") == "utf-8"
        assert result is not None

    def test_manifest_parsed_as_json(self):
        """If manifest file is found, it is parsed as JSON and returned.
        Kills mutmut variants that skip JSON parsing."""
        manifest_data = {"src/main.tsx": {"file": "hashed.js", "css": ["hashed.css"]}}
        manifest_mock = mock.MagicMock()
        manifest_mock.is_file.return_value = True
        manifest_mock.read_text.return_value = json.dumps(manifest_data)
        vite_mock = mock.MagicMock()
        vite_mock.__truediv__ = lambda self, n: manifest_mock
        frontend_mock = mock.MagicMock()
        frontend_mock.__truediv__ = lambda self, n: vite_mock

        with mock.patch("importlib.resources.files") as mf:
            mf.return_value = mock.MagicMock(__truediv__=lambda self, n: frontend_mock)
            result = ui._read_vite_manifest()

        assert result is not None
        assert "src/main.tsx" in result
        assert result["src/main.tsx"]["file"] == "hashed.js"


# ---------------------------------------------------------------------------
# _vite_entry_tags mutation killers
# mutmut_10: wrong CSS href format
# mutmut_13: wrong JS src format
# mutmut_20: missing type='module' for script
# ---------------------------------------------------------------------------


class TestViteEntryTagsMutationKilling:
    def test_css_tag_has_link_rel_stylesheet(self):
        """CSS entries produce <link rel='stylesheet' href='...'> tags (mutmut_10)."""
        ui._vite_manifest = {"src/main.tsx": {"file": "main.js", "css": ["style.css"]}}
        ui._vite_manifest_loaded = True
        tags = _vite_entry_tags("/assets")
        assert "rel='stylesheet'" in tags
        assert "style.css" in tags

    def test_css_tag_uses_assets_path_prefix(self):
        """CSS href is prefixed with assets_path (kills missing-prefix mutants)."""
        ui._vite_manifest = {"src/main.tsx": {"file": "main.js", "css": ["app.css"]}}
        ui._vite_manifest_loaded = True
        tags = _vite_entry_tags("/myassets")
        assert "/myassets/app.css" in tags

    def test_js_tag_type_module(self):
        """JS entry produces <script type='module' src='...'> (mutmut_20: missing type)."""
        ui._vite_manifest = {"src/main.tsx": {"file": "main-abc.js"}}
        ui._vite_manifest_loaded = True
        tags = _vite_entry_tags("/assets")
        assert "type='module'" in tags
        assert "main-abc.js" in tags

    def test_js_tag_uses_assets_path_prefix(self):
        """JS src is prefixed with assets_path (mutmut_13: missing prefix)."""
        ui._vite_manifest = {"src/main.tsx": {"file": "bundle.js"}}
        ui._vite_manifest_loaded = True
        tags = _vite_entry_tags("/static")
        assert "/static/bundle.js" in tags


# ---------------------------------------------------------------------------
# _shell mutation killers
# mutmut_1-3: title not escaped
# mutmut_8: legacy CSS not shown when no vite
# mutmut_10-12: CSS/script ordering wrong
# mutmut_15: scripts dropped in vite mode
# mutmut_20-31: xterm/fitaddon/fonts CDN broken
# mutmut_38-42: body/scripts positioning wrong
# ---------------------------------------------------------------------------


class TestShellMutationKilling:
    def setup_method(self):
        """Disable vite for legacy mode tests."""
        ui._vite_manifest = None
        ui._vite_manifest_loaded = True

    def test_title_included_in_html(self):
        """Title appears in <title> tag (mutmut_1-3)."""
        html = _shell("My Title", "/assets", "<body></body>")
        assert "My Title" in html

    def test_title_escaped(self):
        """Title is HTML-escaped (mutmut_1 may skip escaping)."""
        html = _shell("<script>alert(1)</script>", "/assets", "<body></body>")
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_legacy_css_included_when_no_vite(self):
        """Legacy CSS files included when no vite manifest (mutmut_8)."""
        html = _shell("T", "/assets", "<body></body>")
        assert "server-app-foundation.css" in html
        assert "server-app-layout.css" in html
        assert "server-app-components.css" in html
        assert "server-app-views.css" in html

    def test_legacy_css_not_included_when_vite(self):
        """Legacy CSS NOT included when vite manifest present."""
        ui._vite_manifest = {"src/main.tsx": {"file": "main.js", "css": []}}
        html = _shell("T", "/assets", "<body></body>")
        assert "server-app-foundation.css" not in html

    def test_xterm_cdn_css_included(self):
        """xterm CDN CSS tag included when xterm_cdn provided (mutmut_20)."""
        html = _shell("T", "/assets", "<body></body>", xterm_cdn="https://cdn.example.com/xterm")
        assert "xterm.css" in html
        assert "cdn.example.com/xterm" in html

    def test_xterm_cdn_js_included(self):
        """xterm CDN JS tag included when xterm_cdn provided (mutmut_23)."""
        html = _shell("T", "/assets", "<body></body>", xterm_cdn="https://cdn.example.com/xterm")
        assert "xterm.js" in html

    def test_fitaddon_cdn_js_included(self):
        """fitaddon CDN JS tag included when fitaddon_cdn provided (mutmut_26)."""
        html = _shell("T", "/assets", "<body></body>", fitaddon_cdn="https://cdn.example.com/fit")
        assert "addon-fit.js" in html

    def test_fonts_cdn_link_included(self):
        """Fonts CDN link included when fonts_cdn provided (mutmut_29)."""
        html = _shell("T", "/assets", "<body></body>", fonts_cdn="https://fonts.example.com/font")
        assert "fonts.example.com/font" in html
        assert "rel='stylesheet'" in html

    def test_script_tag_module_type(self):
        """scripts param produces type='module' script tags (mutmut_38-42)."""
        html = _shell("T", "/assets", "<body></body>", scripts=("my-script.js",))
        assert "type='module'" in html
        assert "my-script.js" in html

    def test_extra_css_included(self):
        """extra_css param produces link tags."""
        html = _shell("T", "/assets", "<body></body>", extra_css=("extra.css",))
        assert "extra.css" in html

    def test_html_structure(self):
        """Produced HTML is valid HTML with doctype and html tag."""
        html = _shell("T", "/assets", "<body></body>")
        assert html.startswith("<!DOCTYPE html>")
        assert "<html>" in html
        assert "</html>" in html

    def test_body_included(self):
        """body parameter content is included in HTML."""
        html = _shell("T", "/assets", "<body><div id='content'>test</div></body>")
        assert "<div id='content'>test</div>" in html

    def test_vite_mode_scripts_cleared(self):
        """When vite manifest present, scripts param is ignored (mutmut_15)."""
        ui._vite_manifest = {"src/main.tsx": {"file": "vite-main.js", "css": []}}
        html = _shell("T", "/assets", "<body></body>", scripts=("server-session-page.js",))
        assert "server-session-page.js" not in html
        assert "vite-main.js" in html

    def test_no_xterm_cdn_no_tag(self):
        """Without xterm_cdn, no xterm.js or xterm.css tag."""
        html = _shell("T", "/assets", "<body></body>")
        assert "xterm.js" not in html
        assert "xterm.css" not in html


# ---------------------------------------------------------------------------
# _bootstrap_tag mutation killers
# mutmut_7: replace "</script>" → something else
# mutmut_8: id='app-bootstrap' changed
# ---------------------------------------------------------------------------


class TestBootstrapTagMutationKilling:
    def test_contains_app_bootstrap_id(self):
        """Tag must have id='app-bootstrap' (mutmut_8)."""
        tag = _bootstrap_tag({"key": "value"})
        assert "id='app-bootstrap'" in tag

    def test_contains_json_payload(self):
        """Tag contains JSON-encoded payload."""
        payload = {"page_kind": "test", "title": "My Page"}
        tag = _bootstrap_tag(payload)
        assert '"page_kind"' in tag
        assert '"test"' in tag

    def test_escapes_closing_script_tag(self):
        """Closing </script> in JSON is escaped (mutmut_7: replace("</" → ...) broken)."""
        # A payload with a </script> value would be dangerous if not escaped
        payload = {"html": "</script><script>evil()"}
        tag = _bootstrap_tag(payload)
        # The </script> should be escaped as <\/script>
        assert "</script>" not in tag or "<\\/script>" in tag

    def test_type_application_json(self):
        """Tag type is 'application/json'."""
        tag = _bootstrap_tag({"x": 1})
        assert "type='application/json'" in tag


# ---------------------------------------------------------------------------
# operator_dashboard_html mutation killers
# Tests that the bootstrap and structure are correct
# ---------------------------------------------------------------------------


class TestOperatorDashboardHtmlMutationKilling:
    def test_page_kind_dashboard(self):
        """Bootstrap has page_kind='dashboard' (mutmut_1/2/3)."""
        html = operator_dashboard_html("Title", "/app", "/assets")
        assert '"page_kind": "dashboard"' in html or "page_kind" in html
        # The bootstrap tag contains JSON; extract it
        assert "dashboard" in html

    def test_title_in_bootstrap(self):
        """Bootstrap contains the title (mutmut_9/10)."""
        html = operator_dashboard_html("MyBoardTitle", "/app", "/assets")
        assert "MyBoardTitle" in html

    def test_app_path_in_bootstrap(self):
        """Bootstrap contains the app_path (mutmut_11/12)."""
        html = operator_dashboard_html("T", "/my_app_path", "/assets")
        assert "/my_app_path" in html

    def test_assets_path_in_bootstrap(self):
        """Bootstrap contains the assets_path (mutmut_13/14)."""
        html = operator_dashboard_html("T", "/app", "/my_assets_path")
        assert "/my_assets_path" in html

    def test_app_root_div_present(self):
        """HTML contains <div id='app-root'>."""
        html = operator_dashboard_html("T", "/app", "/assets")
        assert "<div id='app-root'>" in html

    def test_xterm_cdn_forwarded(self):
        """xterm_cdn is passed to _shell (mutmut_20/21)."""
        html = operator_dashboard_html("T", "/app", "/assets", xterm_cdn="https://cdn.x.com")
        assert "cdn.x.com" in html


# (TestSessionPageHtmlMutationKilling, TestConnectPageHtmlMutationKilling,
#  TestReplayPageHtmlMutationKilling moved to test_ui_mutation_killing_2.py)
