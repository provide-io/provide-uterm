#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for server/pages.py — HTML page generators."""

from __future__ import annotations

import json
import re

from provide.terminal.server import ui


class TestBootstrapTagMutants:
    """Kill mutations in _bootstrap_tag."""

    def test_script_closing_tag_escaped_correctly(self):
        """mutmut_7: replace('</', ...) must match '</' not 'XX</XX'."""
        payload = {"url": "https://example.com/path</script>injection"}
        tag = ui._bootstrap_tag(payload)
        # The JSON content inside the tag must have '</' escaped
        # Extract just the JSON content (between the opening tag and the final </script>)
        prefix = "<script type='application/json' id='app-bootstrap'>"
        suffix = "</script>"
        assert tag.startswith(prefix)
        assert tag.endswith(suffix)
        json_content = tag[len(prefix) : -len(suffix)]
        # The raw '</script>' string must not appear in the JSON content
        assert "</script>" not in json_content, "Unescaped </script> found in JSON content"
        assert "\\/" in json_content, "Expected escaped '\\/' in JSON content (XSS escape)"

    def test_replacement_target_is_standard_close(self):
        """mutmut_7/8: both source and replacement must be correct."""
        payload = {"key": "value</injected>"}
        tag = ui._bootstrap_tag(payload)
        # Extract JSON content (before the final </script>)
        prefix = "<script type='application/json' id='app-bootstrap'>"
        suffix = "</script>"
        json_content = tag[len(prefix) : -len(suffix)]
        # raw '</' should be replaced with '<\/' in the JSON content
        assert "</" not in json_content, "Unescaped </ in JSON content (wrong replace target)"

    def test_bootstrap_tag_structure(self):
        """mutmut_8: '</' replacement target correct."""
        payload = {"page_kind": "dashboard", "title": "Test"}
        tag = ui._bootstrap_tag(payload)
        assert tag.startswith("<script type='application/json' id='app-bootstrap'>")
        assert tag.endswith("</script>")
        # Parse back the JSON
        inner = tag[len("<script type='application/json' id='app-bootstrap'>") : -len("</script>")]
        inner = inner.replace("<\\/", "</")
        data = json.loads(inner)
        assert data["page_kind"] == "dashboard"


# ---------------------------------------------------------------------------
# operator_dashboard_html — bootstrap key mutations
# ---------------------------------------------------------------------------


class TestOperatorDashboardHtmlMutants:
    """Kill mutations in operator_dashboard_html."""

    def _parse_bootstrap(self, html: str) -> dict:
        """Extract and parse the app-bootstrap JSON from page HTML."""
        match = re.search(r"<script type='application/json' id='app-bootstrap'>(.*?)</script>", html, re.DOTALL)
        assert match, "No app-bootstrap script found"
        raw = match.group(1).replace("<\\/", "</")
        return json.loads(raw)

    def test_default_cdn_params_are_empty_strings(self):
        """mutmut_1/2/3: default xterm/fitaddon/fonts CDN must be '' → no CDN tags injected."""
        html = ui.operator_dashboard_html("My Dashboard", "/app", "/assets")
        assert "XXXX" not in html

    def test_bootstrap_has_page_kind_dashboard(self):
        """Bootstrap page_kind must be 'dashboard'."""
        html = ui.operator_dashboard_html("Test", "/app", "/assets")
        data = self._parse_bootstrap(html)
        assert data["page_kind"] == "dashboard"

    def test_bootstrap_title_key_exact(self):
        """mutmut_9/10: 'title' key must be exact (not 'XXtitleXX' or 'TITLE')."""
        html = ui.operator_dashboard_html("My Title", "/app", "/assets")
        data = self._parse_bootstrap(html)
        assert "title" in data, "Missing 'title' key in bootstrap"
        assert data["title"] == "My Title"

    def test_bootstrap_app_path_key_exact(self):
        """mutmut_11/12: 'app_path' key must be exact."""
        html = ui.operator_dashboard_html("T", "/my-app", "/assets")
        data = self._parse_bootstrap(html)
        assert "app_path" in data, "Missing 'app_path' key in bootstrap"
        assert data["app_path"] == "/my-app"

    def test_bootstrap_assets_path_key_exact(self):
        """mutmut_13/14: 'assets_path' key must be exact."""
        html = ui.operator_dashboard_html("T", "/app", "/my-assets")
        data = self._parse_bootstrap(html)
        assert "assets_path" in data, "Missing 'assets_path' key in bootstrap"
        assert data["assets_path"] == "/my-assets"

    def test_body_contains_app_root_div(self):
        """mutmut_16/17/18: <body> and <div id='app-root'> must be exact."""
        html = ui.operator_dashboard_html("T", "/app", "/assets")
        assert "<body>" in html, "Missing <body> tag"
        assert "<div id='app-root'></div>" in html, "Missing app-root div"

    def test_body_contains_noscript(self):
        """mutmut_20/21/22: noscript block must be present and exact."""
        html = ui.operator_dashboard_html("T", "/app", "/assets")
        assert "<noscript>" in html.lower()
        assert "This application requires JavaScript." in html

    def test_body_ends_with_body_close(self):
        """mutmut_24/25: </body> must be exact lowercase."""
        html = ui.operator_dashboard_html("T", "/app", "/assets")
        assert "</body>" in html, "Missing </body> tag"
        assert "</BODY>" not in html

    def test_fonts_cdn_passed_through(self):
        """mutmut_32/39: fonts_cdn must be forwarded, not None/dropped."""
        html = ui.operator_dashboard_html("T", "/app", "/assets", fonts_cdn="https://fonts.google.com/foo")
        assert "https://fonts.google.com/foo" in html

    def test_session_page_js_script_tag_present_without_vite(self):
        """mutmut_40: scripts=('server-session-page.js',) must be exact."""
        ui._vite_manifest = None
        ui._vite_manifest_loaded = True
        html = ui.operator_dashboard_html("T", "/app", "/assets")
        assert "server-session-page.js" in html, "Missing server-session-page.js script"


# ---------------------------------------------------------------------------
# session_page_html — bootstrap key and structure mutations
# ---------------------------------------------------------------------------


class TestSessionPageHtmlMutants:
    """Kill mutations in session_page_html."""

    def _parse_bootstrap(self, html: str) -> dict:
        match = re.search(r"<script type='application/json' id='app-bootstrap'>(.*?)</script>", html, re.DOTALL)
        assert match, "No app-bootstrap script found"
        raw = match.group(1).replace("<\\/", "</")
        return json.loads(raw)

    def test_default_cdn_empty_strings(self):
        """mutmut_1/2/3: default CDN params must be '' → no CDN tags."""
        html = ui.session_page_html("T", "/assets", "sess-1", operator=False, app_path="/app")
        assert "XXXX" not in html

    def test_bootstrap_title_key_exact(self):
        """mutmut_11/12: 'title' key exact."""
        html = ui.session_page_html("My Title", "/assets", "sess-1", operator=False, app_path="/app")
        data = self._parse_bootstrap(html)
        assert "title" in data
        assert data["title"] == "My Title"

    def test_bootstrap_app_path_key_exact(self):
        """mutmut_13/14: 'app_path' key exact."""
        html = ui.session_page_html("T", "/assets", "sess-1", operator=False, app_path="/my-app")
        data = self._parse_bootstrap(html)
        assert "app_path" in data
        assert data["app_path"] == "/my-app"

    def test_bootstrap_assets_path_key_exact(self):
        """mutmut_15/16: 'assets_path' key exact."""
        html = ui.session_page_html("T", "/my-assets", "sess-1", operator=False, app_path="/app")
        data = self._parse_bootstrap(html)
        assert "assets_path" in data
        assert data["assets_path"] == "/my-assets"

    def test_bootstrap_session_id_key_exact(self):
        """mutmut_17/18: 'session_id' key exact."""
        html = ui.session_page_html("T", "/assets", "my-session", operator=False, app_path="/app")
        data = self._parse_bootstrap(html)
        assert "session_id" in data
        assert data["session_id"] == "my-session"

    def test_bootstrap_surface_key_exact(self):
        """mutmut_19/20: 'surface' key exact (not 'XXsurfaceXX' or 'SURFACE')."""
        html = ui.session_page_html("T", "/assets", "sess-1", operator=False, app_path="/app")
        data = self._parse_bootstrap(html)
        assert "surface" in data

    def test_bootstrap_surface_operator_value_for_operator(self):
        """mutmut_21/22: surface='operator' for operator=True (not 'XXoperatorXX'/'OPERATOR')."""
        html = ui.session_page_html("T", "/assets", "sess-1", operator=True, app_path="/app")
        data = self._parse_bootstrap(html)
        assert data["surface"] == "operator"

    def test_bootstrap_surface_user_value_for_non_operator(self):
        """mutmut_23/24: surface='user' for operator=False (not 'XXuserXX'/'USER')."""
        html = ui.session_page_html("T", "/assets", "sess-1", operator=False, app_path="/app")
        data = self._parse_bootstrap(html)
        assert data["surface"] == "user"

    def test_page_kind_operator_when_operator_true(self):
        """page_kind must be 'operator' when operator=True."""
        html = ui.session_page_html("T", "/assets", "sess-1", operator=True, app_path="/app")
        data = self._parse_bootstrap(html)
        assert data["page_kind"] == "operator"

    def test_page_kind_session_when_operator_false(self):
        """page_kind must be 'session' when operator=False."""
        html = ui.session_page_html("T", "/assets", "sess-1", operator=False, app_path="/app")
        data = self._parse_bootstrap(html)
        assert data["page_kind"] == "session"

    def test_body_contains_app_root_div(self):
        """mutmut_26/27/28/29: body structure exact."""
        html = ui.session_page_html("T", "/assets", "s", operator=False, app_path="/app")
        assert "<body>" in html
        assert "<div id='app-root'></div>" in html

    def test_noscript_exact_case(self):
        """mutmut_30/31/32: noscript block exact."""
        html = ui.session_page_html("T", "/assets", "s", operator=False, app_path="/app")
        assert "This application requires JavaScript." in html

    def test_body_close_lowercase(self):
        """mutmut_35/36: </body> must be lowercase."""
        html = ui.session_page_html("T", "/assets", "s", operator=False, app_path="/app")
        assert "</body>" in html
        assert "</BODY>" not in html

    def test_session_page_script_present_without_vite(self):
        """mutmut_40/47/51/52: scripts=('server-session-page.js',) exact."""
        ui._vite_manifest = None
        ui._vite_manifest_loaded = True
        html = ui.session_page_html("T", "/assets", "s", operator=False, app_path="/app")
        assert "server-session-page.js" in html

    def test_fonts_cdn_forwarded(self):
        """mutmut_43/50: fonts_cdn must be passed through (not None/dropped)."""
        html = ui.session_page_html(
            "T", "/assets", "s", operator=False, app_path="/app", fonts_cdn="https://fonts.google.com/css"
        )
        assert "https://fonts.google.com/css" in html


# ---------------------------------------------------------------------------
# connect_page_html — bootstrap key and structure mutations
# ---------------------------------------------------------------------------


class TestConnectPageHtmlMutants:
    """Kill mutations in connect_page_html."""

    def _parse_bootstrap(self, html: str) -> dict:
        match = re.search(r"<script type='application/json' id='app-bootstrap'>(.*?)</script>", html, re.DOTALL)
        assert match
        raw = match.group(1).replace("<\\/", "</")
        return json.loads(raw)

    def test_default_cdn_empty(self):
        """mutmut_1/2/3: default CDN empty."""
        html = ui.connect_page_html("T", "/assets", "/app")
        assert "XXXX" not in html

    def test_bootstrap_page_kind_connect(self):
        html = ui.connect_page_html("T", "/assets", "/app")
        data = self._parse_bootstrap(html)
        assert data["page_kind"] == "connect"

    def test_bootstrap_title_key_exact(self):
        """mutmut_9/10: 'title' key exact."""
        html = ui.connect_page_html("My Connect", "/assets", "/app")
        data = self._parse_bootstrap(html)
        assert "title" in data
        assert data["title"] == "My Connect"

    def test_bootstrap_app_path_key_exact(self):
        """mutmut_11/12: 'app_path' key exact."""
        html = ui.connect_page_html("T", "/assets", "/my-app")
        data = self._parse_bootstrap(html)
        assert "app_path" in data
        assert data["app_path"] == "/my-app"

    def test_bootstrap_assets_path_key_exact(self):
        """mutmut_13/14: 'assets_path' key exact."""
        html = ui.connect_page_html("T", "/my-assets", "/app")
        data = self._parse_bootstrap(html)
        assert "assets_path" in data
        assert data["assets_path"] == "/my-assets"

    def test_body_contains_body_tag(self):
        """mutmut_16/17: <body> exact."""
        html = ui.connect_page_html("T", "/assets", "/app")
        assert "<body>" in html

    def test_body_contains_app_root_div(self):
        """mutmut_18/19: <div id='app-root'> exact."""
        html = ui.connect_page_html("T", "/assets", "/app")
        assert "<div id='app-root'></div>" in html

    def test_body_contains_noscript(self):
        """mutmut_20/21/22: noscript exact case."""
        html = ui.connect_page_html("T", "/assets", "/app")
        assert "This application requires JavaScript." in html

    def test_body_close_lowercase(self):
        """mutmut_24/25: </body> lowercase."""
        html = ui.connect_page_html("T", "/assets", "/app")
        assert "</body>" in html

    def test_session_page_script_present_without_vite(self):
        """mutmut_29/36/40/41: scripts=('server-session-page.js',) exact."""
        ui._vite_manifest = None
        ui._vite_manifest_loaded = True
        html = ui.connect_page_html("T", "/assets", "/app")
        assert "server-session-page.js" in html

    def test_xterm_cdn_forwarded(self):
        """mutmut_30/37: xterm_cdn passed through (not None/dropped)."""
        html = ui.connect_page_html("T", "/assets", "/app", xterm_cdn="https://cdn.xterm.org")
        assert "xterm.js" in html

    def test_fonts_cdn_forwarded(self):
        """mutmut_32/39: fonts_cdn passed through."""
        html = ui.connect_page_html("T", "/assets", "/app", fonts_cdn="https://fonts.google.com/foo")
        assert "https://fonts.google.com/foo" in html


# ---------------------------------------------------------------------------
# replay_page_html — bootstrap key and structure mutations
# ---------------------------------------------------------------------------


class TestReplayPageHtmlMutants:
    """Kill mutations in replay_page_html."""

    def _parse_bootstrap(self, html: str) -> dict:
        match = re.search(r"<script type='application/json' id='app-bootstrap'>(.*?)</script>", html, re.DOTALL)
        assert match
        raw = match.group(1).replace("<\\/", "</")
        return json.loads(raw)

    def test_default_cdn_empty(self):
        """mutmut_1/2/3: default CDN empty strings."""
        html = ui.replay_page_html("T", "/assets", "sess-1", app_path="/app")
        assert "XXXX" not in html

    def test_bootstrap_page_kind_replay(self):
        html = ui.replay_page_html("T", "/assets", "sess-1", app_path="/app")
        data = self._parse_bootstrap(html)
        assert data["page_kind"] == "replay"

    def test_bootstrap_title_key_exact(self):
        """mutmut_9/10: 'title' key exact."""
        html = ui.replay_page_html("My Replay", "/assets", "sess-1", app_path="/app")
        data = self._parse_bootstrap(html)
        assert "title" in data
        assert data["title"] == "My Replay"

    def test_bootstrap_assets_path_key_exact(self):
        """mutmut_13/14: 'assets_path' key exact."""
        html = ui.replay_page_html("T", "/my-assets", "sess-1", app_path="/app")
        data = self._parse_bootstrap(html)
        assert "assets_path" in data
        assert data["assets_path"] == "/my-assets"

    def test_bootstrap_session_id_key_exact(self):
        """mutmut_15/16: 'session_id' key exact."""
        html = ui.replay_page_html("T", "/assets", "my-sess", app_path="/app")
        data = self._parse_bootstrap(html)
        assert "session_id" in data
        assert data["session_id"] == "my-sess"

    def test_bootstrap_surface_key_exact(self):
        """mutmut_17/18: 'surface' key exact."""
        html = ui.replay_page_html("T", "/assets", "s", app_path="/app")
        data = self._parse_bootstrap(html)
        assert "surface" in data

    def test_bootstrap_surface_value_is_operator(self):
        """mutmut_19/20: surface must be 'operator' (not 'XXoperatorXX'/'OPERATOR')."""
        html = ui.replay_page_html("T", "/assets", "s", app_path="/app")
        data = self._parse_bootstrap(html)
        assert data["surface"] == "operator"

    def test_body_contains_body_tag(self):
        """mutmut_22/23: <body> exact."""
        html = ui.replay_page_html("T", "/assets", "s", app_path="/app")
        assert "<body>" in html

    def test_body_contains_app_root_div(self):
        """mutmut_24/25: <div id='app-root'> exact."""
        html = ui.replay_page_html("T", "/assets", "s", app_path="/app")
        assert "<div id='app-root'></div>" in html

    def test_body_contains_noscript(self):
        """mutmut_26/27/28: noscript exact."""
        html = ui.replay_page_html("T", "/assets", "s", app_path="/app")
        assert "This application requires JavaScript." in html

    def test_body_close_lowercase(self):
        """mutmut_30/31: </body> lowercase."""
        html = ui.replay_page_html("T", "/assets", "s", app_path="/app")
        assert "</body>" in html

    def test_title_appended_with_replay(self):
        """Title passed to _shell must be '{title} Replay'."""
        html = ui.replay_page_html("My Session", "/assets", "s", app_path="/app")
        assert "<title>My Session Replay</title>" in html

    def test_replay_page_script_present_without_vite(self):
        """mutmut_35/42/46/47: scripts=('server-replay-page.js',) exact."""
        ui._vite_manifest = None
        ui._vite_manifest_loaded = True
        html = ui.replay_page_html("T", "/assets", "s", app_path="/app")
        assert "server-replay-page.js" in html

    def test_fonts_cdn_forwarded(self):
        """mutmut_38/45: fonts_cdn passed through (not None/dropped)."""
        html = ui.replay_page_html("T", "/assets", "s", app_path="/app", fonts_cdn="https://fonts.example.com/f")
        assert "https://fonts.example.com/f" in html


# ---------------------------------------------------------------------------
# create_server_app — metrics dict mutations
# ---------------------------------------------------------------------------
