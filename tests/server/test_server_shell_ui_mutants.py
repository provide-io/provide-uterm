#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for server/ui.py — shell() function."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from provide.terminal.server import ui
from provide.terminal.server.app import create_server_app
from provide.terminal.server.models import AuthConfig, ServerBindConfig, ServerConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_vite_cache():
    """Reset the cached Vite manifest between every test."""
    ui._vite_manifest = None
    ui._vite_manifest_loaded = False
    yield
    ui._vite_manifest = None
    ui._vite_manifest_loaded = False


def _make_app(mode: str = "dev", allowed_origins: list[str] | None = None) -> TestClient:
    config = ServerConfig(auth=AuthConfig(mode=mode))
    if allowed_origins is not None:
        config.server = ServerBindConfig(allowed_origins=allowed_origins)
    app = create_server_app(config)
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# _validate_frontend_assets — path mutations
# ---------------------------------------------------------------------------


class TestShellMutants:
    """Kill mutations in _shell."""

    def _call(self, **kwargs) -> str:
        defaults = {
            "title": "Test",
            "assets_path": "/assets",
            "body": "<body>test</body>",
        }
        defaults.update(kwargs)
        return ui._shell(**defaults)

    def test_xterm_cdn_default_empty_means_no_cdn_tags(self):
        """mutmut_1/2/3: default xterm/fitaddon/fonts CDN must be '' (not 'XXXX')."""
        html = ui._shell("T", "/a", "<body></body>")
        assert "XXXX" not in html, "Default CDN params must be empty strings"

    def test_css_links_uses_empty_join_separator(self):
        """mutmut_8/10: css_links must use '' separator (not None / 'XXXX')."""
        # Force no-vite mode so extra_css is used
        ui._vite_manifest = None
        ui._vite_manifest_loaded = True
        html = ui._shell(
            "T",
            "/assets",
            "<body></body>",
            extra_css=("foo.css", "bar.css"),
        )
        # The join separator should not appear between link tags
        assert "XXXX" not in html
        assert "None" not in html
        assert "foo.css" in html
        assert "bar.css" in html

    def test_css_links_uses_correct_assets_path(self):
        """mutmut_11: escape(None) instead of escape(assets_path) -> 'None' in href."""
        # Force no-vite mode so extra_css is used
        ui._vite_manifest = None
        ui._vite_manifest_loaded = True
        html = ui._shell(
            "T",
            "/my-assets",
            "<body></body>",
            extra_css=("style.css",),
        )
        assert "/my-assets/style.css" in html, "assets_path must appear in CSS link href"
        assert "None/style.css" not in html

    def test_css_link_href_uses_correct_filename(self):
        """mutmut_12: escape(None) for filename -> 'None' in href."""
        # Force no-vite mode so extra_css is used
        ui._vite_manifest = None
        ui._vite_manifest_loaded = True
        html = ui._shell(
            "T",
            "/assets",
            "<body></body>",
            extra_css=("my-style.css",),
        )
        assert "my-style.css" in html
        assert "/assets/None" not in html

    def test_script_tags_uses_empty_join_separator(self):
        """mutmut_15: 'XXXX'.join -> garbage between script tags."""
        # Force no-vite mode so scripts are used
        ui._vite_manifest = None
        ui._vite_manifest_loaded = True
        html = ui._shell(
            "T",
            "/assets",
            "<body></body>",
            scripts=("app.js", "extra.js"),
        )
        assert "XXXX" not in html
        assert "app.js" in html
        assert "extra.js" in html

    def test_no_xterm_cdn_means_no_xterm_css_tag(self):
        """mutmut_20: empty fallback 'XXXX' instead of '' -> false xterm CSS output."""
        html = ui._shell("T", "/a", "<body></body>", xterm_cdn="")
        # When xterm_cdn is empty string, no xterm CSS should appear
        assert "xterm.css" not in html
        assert "XXXX" not in html

    def test_no_xterm_cdn_means_no_xterm_js_tag(self):
        """mutmut_23: empty fallback 'XXXX' instead of '' -> false xterm JS output."""
        html = ui._shell("T", "/a", "<body></body>", xterm_cdn="")
        assert "xterm.js" not in html
        assert "XXXX" not in html

    def test_no_fitaddon_cdn_means_no_fitaddon_js(self):
        """mutmut_26: empty fallback 'XXXX' instead of '' -> false fitaddon output."""
        html = ui._shell("T", "/a", "<body></body>", fitaddon_cdn="")
        assert "addon-fit.js" not in html
        assert "XXXX" not in html

    def test_no_fonts_cdn_means_no_fonts_link(self):
        """mutmut_27/29: fonts_link=None/'XXXX' instead of '' -> garbage in output."""
        html = ui._shell("T", "/a", "<body></body>", fonts_cdn="")
        assert "XXXX" not in html
        assert "None" not in html.replace("uterm_principal", "").replace("None-", "")
        # No <link href> for fonts should appear
        assert "rel='stylesheet'" not in html or "xterm" not in html

    def test_legacy_css_initialised_empty_before_conditional(self):
        """mutmut_30/31: legacy_css initialised as None/'XXXX' would pollute Vite output."""
        # With vite manifest active, legacy_css must be empty (not 'XXXX' or None)
        ui._vite_manifest = {"src/main.tsx": {"file": "main.js", "css": []}}
        ui._vite_manifest_loaded = True
        html = ui._shell("T", "/assets", "<body></body>")
        assert "XXXX" not in html
        assert html.count("None") == 0 or "None" not in html

    def test_html_starts_with_doctype(self):
        """mutmut_38/39/40: DOCTYPE must not be mangled."""
        html = self._call()
        assert html.startswith("<!DOCTYPE html>"), f"Bad HTML start: {html[:40]!r}"

    def test_meta_viewport_present_exact_case(self):
        """mutmut_41/42: viewport meta must be lowercase."""
        html = self._call()
        assert "<meta name='viewport'" in html, "Viewport meta tag missing or wrong case"
        assert "width=device-width" in html

    def test_with_xterm_cdn(self):
        """Ensure xterm CDN injects correct tags."""
        html = ui._shell("T", "/a", "<body></body>", xterm_cdn="https://cdn.xterm.org")
        assert "xterm.css" in html
        assert "xterm.js" in html
        assert "https://cdn.xterm.org" in html

    def test_with_fitaddon_cdn(self):
        """Ensure fitaddon CDN injects correct tags."""
        html = ui._shell("T", "/a", "<body></body>", fitaddon_cdn="https://cdn.fit.org")
        assert "addon-fit.js" in html
        assert "https://cdn.fit.org" in html

    def test_with_fonts_cdn(self):
        """Ensure fonts CDN injects correct link tag."""
        html = ui._shell("T", "/a", "<body></body>", fonts_cdn="https://fonts.google.com/foo")
        assert "https://fonts.google.com/foo" in html
        assert "rel='stylesheet'" in html


# ---------------------------------------------------------------------------
# _bootstrap_tag — XSS escape mutations
# ---------------------------------------------------------------------------
