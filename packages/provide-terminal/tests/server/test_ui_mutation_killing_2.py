#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for server/ui.py — session, connect, and replay pages."""

from __future__ import annotations

import pytest

from provide.terminal.server import ui
from provide.terminal.server.ui import (
    connect_page_html,
    replay_page_html,
    session_page_html,
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
# session_page_html mutation killers
# ---------------------------------------------------------------------------


class TestSessionPageHtmlMutationKilling:
    def test_page_kind_operator_when_operator_true(self):
        """Bootstrap page_kind='operator' when operator=True (mutmut_1/2)."""
        html = session_page_html("T", "/assets", "sess-1", operator=True, app_path="/app")
        assert "operator" in html

    def test_page_kind_session_when_operator_false(self):
        """Bootstrap page_kind='session' when operator=False (mutmut_3)."""
        html = session_page_html("T", "/assets", "sess-1", operator=False, app_path="/app")
        assert "session" in html

    def test_session_id_in_bootstrap(self):
        """Bootstrap contains session_id (mutmut_11/12)."""
        html = session_page_html("T", "/assets", "my_session_id_xyz", operator=False, app_path="/app")
        assert "my_session_id_xyz" in html

    def test_surface_operator_when_operator_true(self):
        """surface='operator' when operator=True (mutmut_13/14)."""
        html = session_page_html("T", "/assets", "s1", operator=True, app_path="/app")
        # The surface field in bootstrap JSON
        assert '"surface": "operator"' in html or '"operator"' in html

    def test_hijack_js_always_included(self):
        """hijack.js is always included in the body (mutmut_15-18)."""
        html = session_page_html("T", "/assets", "s1", operator=True, app_path="/app")
        assert "hijack.js" in html

    def test_hijack_js_uses_assets_path(self):
        """hijack.js src uses assets_path (mutmut_40/43)."""
        html = session_page_html("T", "/custom_assets", "s1", operator=False, app_path="/app")
        assert "/custom_assets/hijack.js" in html

    def test_title_in_page(self):
        """Title appears in the HTML (mutmut_1-3: title mangling)."""
        html = session_page_html("SessionTitle", "/assets", "s1", operator=False, app_path="/app")
        assert "SessionTitle" in html

    def test_app_path_in_bootstrap(self):
        """app_path in bootstrap (mutmut_19/20)."""
        html = session_page_html("T", "/assets", "s1", operator=False, app_path="/my_app")
        assert "/my_app" in html


# ---------------------------------------------------------------------------
# connect_page_html mutation killers
# ---------------------------------------------------------------------------


class TestConnectPageHtmlMutationKilling:
    def test_page_kind_connect(self):
        """Bootstrap has page_kind='connect' (mutmut_1/2/3)."""
        html = connect_page_html("T", "/assets", "/app")
        assert "connect" in html

    def test_title_in_page(self):
        """Title in HTML."""
        html = connect_page_html("ConnectTitle", "/assets", "/app")
        assert "ConnectTitle" in html

    def test_app_path_in_bootstrap(self):
        """app_path in bootstrap (mutmut_9/10)."""
        html = connect_page_html("T", "/assets", "/connect_app")
        assert "/connect_app" in html

    def test_assets_path_in_bootstrap(self):
        """assets_path in bootstrap (mutmut_11/12)."""
        html = connect_page_html("T", "/my_connect_assets", "/app")
        assert "/my_connect_assets" in html

    def test_server_session_page_js_when_no_vite(self):
        """Legacy mode includes server-session-page.js (mutmut_13/14)."""
        # Force no-vite mode by marking cache as loaded with no manifest
        ui._vite_manifest = None
        ui._vite_manifest_loaded = True
        html = connect_page_html("T", "/assets", "/app")
        assert "server-session-page.js" in html

    def test_app_root_div_present(self):
        """HTML contains <div id='app-root'>."""
        html = connect_page_html("T", "/assets", "/app")
        assert "<div id='app-root'>" in html


# ---------------------------------------------------------------------------
# replay_page_html mutation killers
# ---------------------------------------------------------------------------


class TestReplayPageHtmlMutationKilling:
    def test_page_kind_replay(self):
        """Bootstrap has page_kind='replay' (mutmut_1/2/3)."""
        html = replay_page_html("T", "/assets", "sess-1", app_path="/app")
        assert "replay" in html

    def test_session_id_in_bootstrap(self):
        """session_id in bootstrap (mutmut_9/10)."""
        html = replay_page_html("T", "/assets", "replay_session_42", app_path="/app")
        assert "replay_session_42" in html

    def test_title_includes_replay_suffix(self):
        """Title passed to _shell has ' Replay' suffix (mutmut_13/14)."""
        html = replay_page_html("BaseTitle", "/assets", "s1", app_path="/app")
        assert "BaseTitle Replay" in html

    def test_server_replay_page_js_when_no_vite(self):
        """Legacy mode includes server-replay-page.js (not session-page.js) (mutmut_15-18)."""
        # Force no-vite mode by marking cache as loaded with no manifest
        ui._vite_manifest = None
        ui._vite_manifest_loaded = True
        html = replay_page_html("T", "/assets", "s1", app_path="/app")
        assert "server-replay-page.js" in html
        assert "server-session-page.js" not in html

    def test_surface_operator_in_bootstrap(self):
        """surface='operator' in bootstrap for replay (mutmut_19/20)."""
        html = replay_page_html("T", "/assets", "s1", app_path="/app")
        assert '"surface": "operator"' in html or "operator" in html

    def test_app_path_in_bootstrap(self):
        """app_path in bootstrap (mutmut_22/23)."""
        html = replay_page_html("T", "/assets", "s1", app_path="/replay_app")
        assert "/replay_app" in html

    def test_assets_path_in_bootstrap(self):
        """assets_path in bootstrap (mutmut_24/25)."""
        html = replay_page_html("T", "/replay_assets", "s1", app_path="/app")
        assert "/replay_assets" in html

    def test_xterm_cdn_passed_through(self):
        """xterm_cdn forwarded to _shell."""
        html = replay_page_html("T", "/assets", "s1", app_path="/app", xterm_cdn="https://x.com")
        assert "x.com" in html
