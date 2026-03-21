#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Targeted mutation-killing tests for server/app.py and server/ui.py.

Each test is designed to pass with the original source but fail with a
specific surviving mutant.  Tests are grouped by source function.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from provide.terminal.server import ui
from provide.terminal.server.app import _validate_frontend_assets, create_server_app
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


class TestValidateFrontendAssetsPathMutants:
    """Kill path-string mutations in _validate_frontend_assets."""

    def test_uses_frontend_dir_not_frontend_uppercase(self, tmp_path):
        """mutmut_7: 'FRONTEND' path swap — should raise because 'FRONTEND' dir is absent."""
        # The real function must look at 'frontend', not 'FRONTEND'.
        # We can verify by patching resources to return a mock that succeeds
        # only for 'frontend'.

        # Build a fake traversable that only serves 'frontend' correctly
        class FakeFile:
            def __init__(self, exists: bool = True):
                self._exists = exists

            def is_file(self):
                return self._exists

        class FakeFrontend:
            def __truediv__(self, name):
                # Only serve real sub-paths
                if name in (".vite", "app"):
                    return FakeViteOrApp(name)
                # For hijack.html, terminal.html — simulate they exist
                return FakeFile(True)

        class FakeViteOrApp:
            def __init__(self, parent):
                self._parent = parent

            def __truediv__(self, name):
                # .vite/manifest.json and app/boot.js both exist
                return FakeFile(True)

            def is_file(self):
                return True

        class FakeRoot:
            def __truediv__(self, name):
                if name == "frontend":
                    return FakeFrontend()
                # Wrong directory name — return non-existent
                return FakeFile(False)

        with mock.patch("provide.terminal.server.app.importlib.resources.files", return_value=FakeRoot()):
            # Should not raise when 'frontend' resolves correctly
            _validate_frontend_assets()

    def test_required_files_are_hijack_html_and_terminal_html(self, tmp_path):
        """mutmut_10: 'HIJACK.HTML' swap — required tuple must contain 'hijack.html'."""

        class FakePath:
            """Recursive fake path that tracks the last segment name."""

            def __init__(self, name):
                self._name = name

            def __truediv__(self, name):
                return FakePath(name)

            def is_file(self):
                # Only exact lowercase names succeed
                return self._name in ("hijack.html", "terminal.html", "manifest.json", "boot.js")

        class FakeRoot:
            def __truediv__(self, name):
                return FakePath(name)

        with mock.patch("provide.terminal.server.app.importlib.resources.files", return_value=FakeRoot()):
            # Should succeed: the real function uses 'hijack.html' (lowercase)
            _validate_frontend_assets()

    def test_has_vite_checks_correct_path(self):
        """mutmut_16/19/21/22: has_vite must not be hardcoded None and must use correct path."""

        calls_seen = []

        class FakeFile:
            def __init__(self, name):
                self._name = name

            def __truediv__(self, part):
                calls_seen.append(part)
                return FakeFile(part)

            def is_file(self):
                # Simulate all files exist
                return True

        class FakeRoot:
            def __truediv__(self, name):
                calls_seen.append(name)
                return FakeFile(name)

        with mock.patch("provide.terminal.server.app.importlib.resources.files", return_value=FakeRoot()):
            _validate_frontend_assets()

        # Must navigate to .vite/manifest.json
        assert ".vite" in calls_seen, "Expected .vite directory traversal"
        assert "manifest.json" in calls_seen, "Expected manifest.json lookup"

    def test_has_legacy_checks_app_boot_js(self):
        """mutmut_23/26/27/28/29: has_legacy must navigate to app/boot.js."""

        calls_seen = []

        class FakeFile:
            def __init__(self, name):
                self._name = name

            def __truediv__(self, part):
                calls_seen.append(part)
                return FakeFile(part)

            def is_file(self):
                return True

        class FakeRoot:
            def __truediv__(self, name):
                calls_seen.append(name)
                return FakeFile(name)

        with mock.patch("provide.terminal.server.app.importlib.resources.files", return_value=FakeRoot()):
            _validate_frontend_assets()

        assert "app" in calls_seen, "Expected app directory traversal"
        assert "boot.js" in calls_seen, "Expected boot.js lookup"

    def test_neither_vite_nor_legacy_raises(self):
        """mutmut_30: 'and' vs 'or' — raises only when BOTH vite AND legacy are missing."""

        class FakeFile:
            def __init__(self, name, exists=True):
                self._exists = exists

            def __truediv__(self, part):
                # simulate all required HTML files exist
                return FakeFile(part, exists=True)

            def is_file(self):
                return self._exists

        class FakeDir:
            def __init__(self, name, file_exists=True):
                self._name = name
                self._file_exists = file_exists

            def __truediv__(self, part):
                return FakeFile(part, self._file_exists)

            def is_file(self):
                return self._file_exists

        class FakeRoot:
            def __truediv__(self, name):
                if name == "frontend":
                    return FakeFrontendDir()
                return FakeFile(name, True)

        class FakeFrontendDir:
            def __truediv__(self, name):
                if name in ("hijack.html", "terminal.html"):
                    return FakeFile(name, True)
                # .vite and app both absent → should trigger error
                return FakeDir(name, file_exists=False)

        with (
            mock.patch("provide.terminal.server.app.importlib.resources.files", return_value=FakeRoot()),
            pytest.raises(RuntimeError, match="missing required frontend assets"),
        ):
            _validate_frontend_assets()

    def test_only_vite_present_does_not_raise(self):
        """mutmut_30: when has_vite=True and has_legacy=False, no error expected."""

        class FakeFile:
            def __init__(self, exists):
                self._exists = exists

            def is_file(self):
                return self._exists

        class FakePath:
            def __init__(self, name):
                self._name = name
                self._parts = []

            def __truediv__(self, part):
                child = FakePath(part)
                child._parent_name = self._name
                return child

            def is_file(self):
                # Only the Vite manifest exists
                if hasattr(self, "_parent_name"):
                    if self._name == "manifest.json":
                        return True  # .vite/manifest.json exists
                    if self._name in ("hijack.html", "terminal.html"):
                        return True
                    if self._name == "boot.js":
                        return False  # legacy missing
                return True

        class FakeRoot:
            def __truediv__(self, name):
                return FakePath(name)

        with mock.patch("provide.terminal.server.app.importlib.resources.files", return_value=FakeRoot()):
            # Should NOT raise because vite manifest is present
            _validate_frontend_assets()


# ---------------------------------------------------------------------------
# _read_vite_manifest — cache and loading mutations
# ---------------------------------------------------------------------------


class TestReadViteManifestCacheMutants:
    """Kill mutations in _read_vite_manifest caching logic."""

    def test_loaded_flag_set_to_true_after_first_call(self):
        """mutmut_1/2: _vite_manifest_loaded must be True after first call (not None/False)."""
        with mock.patch("importlib.resources.files", side_effect=Exception("no files")):
            ui._read_vite_manifest()
        # After the call, the flag must be truthy so next call skips reload
        assert ui._vite_manifest_loaded is True

    def test_caching_works_second_call_does_not_reload(self):
        """mutmut_1/2: if loaded=False after first call, second call would reload."""
        call_count = [0]
        _ = __import__("importlib.resources", fromlist=["files"]).files

        def counting_files(package):
            call_count[0] += 1
            raise Exception("boom")

        with mock.patch("importlib.resources.files", counting_files):
            ui._read_vite_manifest()
            ui._read_vite_manifest()

        # Second call must not re-enter the body
        assert call_count[0] == 1, "manifest loader called more than once — caching broken"

    def test_reads_manifest_from_correct_package(self):
        """mutmut_7/8/9: files() must be called with 'provide.terminal' not corrupted string."""
        captured_args = []

        def capturing_files(package):
            captured_args.append(package)
            raise Exception("stop")

        with mock.patch("importlib.resources.files", capturing_files):
            ui._read_vite_manifest()

        assert captured_args == ["provide.terminal"], f"files() called with wrong args: {captured_args}"

    def test_manifest_path_traverses_frontend_dot_vite(self):
        """mutmut_10/11/12/13: path must go through 'frontend' / '.vite' / 'manifest.json'."""
        traversals = []

        class FakePath:
            def __truediv__(self, part):
                traversals.append(part)
                return FakePath()

            def is_file(self):
                return False

        with mock.patch("importlib.resources.files", return_value=FakePath()):
            ui._read_vite_manifest()

        assert "frontend" in traversals, f"Missing 'frontend' in path: {traversals}"
        assert ".vite" in traversals, f"Missing '.vite' in path: {traversals}"
        assert "manifest.json" in traversals, f"Missing 'manifest.json' in path: {traversals}"

    def test_read_text_encoding_is_utf8(self):
        """mutmut_17/18: encoding kwarg to read_text must be 'utf-8' (not None, not 'XXutf-8XX')."""
        read_kwargs = {}

        class FakePath:
            def __truediv__(self, part):
                return FakePath()

            def is_file(self):
                return True

            def read_text(self, **kwargs):
                read_kwargs.update(kwargs)
                return json.dumps({"src/main.tsx": {"file": "main.js"}})

        with mock.patch("importlib.resources.files", return_value=FakePath()):
            result = ui._read_vite_manifest()

        assert result is not None, "Expected manifest to be loaded"
        assert read_kwargs.get("encoding") == "utf-8", f"Wrong encoding: {read_kwargs.get('encoding')!r}"

    def test_manifest_result_stored_in_global(self):
        """mutmut_1/2: after loading, _vite_manifest must hold the parsed dict."""
        data = {"src/main.tsx": {"file": "main-abc.js", "css": []}}

        class FakePath:
            def __truediv__(self, part):
                return FakePath()

            def is_file(self):
                return True

            def read_text(self, **kwargs):
                return json.dumps(data)

        with mock.patch("importlib.resources.files", return_value=FakePath()):
            result = ui._read_vite_manifest()

        assert result is not None
        assert "src/main.tsx" in result
        assert ui._vite_manifest is not None


# ---------------------------------------------------------------------------
# _vite_entry_tags — tag generation mutations
# ---------------------------------------------------------------------------


class TestViteEntryTagsMutants:
    """Kill mutations in _vite_entry_tags."""

    def _set_manifest(self, manifest: dict) -> None:
        ui._vite_manifest = manifest
        ui._vite_manifest_loaded = True

    def test_safe_variable_uses_escaped_assets_path(self):
        """mutmut_10: safe=None would cause '<link href='None/...''."""
        self._set_manifest(
            {
                "src/main.tsx": {
                    "file": "assets/main-abc.js",
                    "css": ["assets/main-def.css"],
                }
            }
        )
        tags = ui._vite_entry_tags("/my-assets")
        assert "None" not in tags, "safe variable must be escaped assets_path, not None"
        assert "/my-assets/" in tags, "assets_path must appear in output"

    def test_tags_initialised_empty_multiple_css(self):
        """mutmut_13: tags='XXXX' would prepend garbage before first CSS link."""
        self._set_manifest(
            {
                "src/main.tsx": {
                    "file": "assets/main.js",
                    "css": ["assets/a.css", "assets/b.css"],
                }
            }
        )
        tags = ui._vite_entry_tags("/assets")
        assert not tags.startswith("XXXX"), "tags variable must start empty, not 'XXXX'"
        assert tags.startswith("<link"), f"Expected tags to start with <link, got: {tags[:30]!r}"

    def test_multiple_css_files_all_appear_in_output(self):
        """mutmut_20: tags= (assignment) instead of tags+= would drop earlier CSS files."""
        self._set_manifest(
            {
                "src/main.tsx": {
                    "file": "assets/main.js",
                    "css": ["assets/a.css", "assets/b.css", "assets/c.css"],
                }
            }
        )
        tags = ui._vite_entry_tags("/assets")
        assert "assets/a.css" in tags, "First CSS file missing — accumulation broken"
        assert "assets/b.css" in tags, "Second CSS file missing — accumulation broken"
        assert "assets/c.css" in tags, "Third CSS file missing — accumulation broken"


# ---------------------------------------------------------------------------
# _shell — output HTML structure mutations
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
        """mutmut_11: escape(None) instead of escape(assets_path) → 'None' in href."""
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
        """mutmut_12: escape(None) for filename → 'None' in href."""
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
        """mutmut_15: 'XXXX'.join → garbage between script tags."""
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
        """mutmut_20: empty fallback 'XXXX' instead of '' → false xterm CSS output."""
        html = ui._shell("T", "/a", "<body></body>", xterm_cdn="")
        # When xterm_cdn is empty string, no xterm CSS should appear
        assert "xterm.css" not in html
        assert "XXXX" not in html

    def test_no_xterm_cdn_means_no_xterm_js_tag(self):
        """mutmut_23: empty fallback 'XXXX' instead of '' → false xterm JS output."""
        html = ui._shell("T", "/a", "<body></body>", xterm_cdn="")
        assert "xterm.js" not in html
        assert "XXXX" not in html

    def test_no_fitaddon_cdn_means_no_fitaddon_js(self):
        """mutmut_26: empty fallback 'XXXX' instead of '' → false fitaddon output."""
        html = ui._shell("T", "/a", "<body></body>", fitaddon_cdn="")
        assert "addon-fit.js" not in html
        assert "XXXX" not in html

    def test_no_fonts_cdn_means_no_fonts_link(self):
        """mutmut_27/29: fonts_link=None/'XXXX' instead of '' → garbage in output."""
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
