#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""HTML helpers for the hosted server UI surfaces."""

from __future__ import annotations

import importlib.resources
import json
from html import escape
from typing import TYPE_CHECKING

from provide.telemetry import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = get_logger(__name__)
# Cached Vite manifest — loaded once on first call.
_vite_manifest: dict[str, object] | None = None
_vite_manifest_loaded = False


def _hijack_js_version() -> str:
    """Return a short cache-busting token based on hijack.js mtime."""
    try:
        path = importlib.resources.files("provide.terminal.server") / "frontend" / "hijack.js"
        if path.is_file():
            return format(int(path.stat().st_mtime_ns), "x")[-8:]  # type: ignore[attr-defined]
    except Exception:
        pass
    return "0"


def _read_vite_manifest() -> dict[str, object] | None:
    """Read the Vite manifest.json from the frontend package-data.

    Returns the parsed manifest dict, or None if the manifest doesn't exist
    (i.e. the React app hasn't been built yet — vanilla-only mode).
    """
    global _vite_manifest, _vite_manifest_loaded
    if _vite_manifest_loaded:
        return _vite_manifest
    _vite_manifest_loaded = True
    try:
        manifest_path = importlib.resources.files("provide.terminal.server") / "frontend" / ".vite" / "manifest.json"
        if manifest_path.is_file():
            raw = manifest_path.read_text(encoding="utf-8")
            _vite_manifest = json.loads(raw)
            logger.info("vite_manifest loaded entries=%d", len(_vite_manifest or {}))
        else:
            logger.debug("vite_manifest not found — using legacy vanilla entry points")
    except Exception:
        logger.debug("vite_manifest read failed — using legacy vanilla entry points", exc_info=True)
    return _vite_manifest


def _vite_entry_tags(assets_path: str) -> str:
    """Return <script>/<link> tags for the Vite React app entry point.

    If the Vite manifest exists, resolves hashed filenames from it.
    Returns empty string if no manifest (vanilla-only mode).
    """
    manifest = _read_vite_manifest()
    if manifest is None:
        return ""
    entry = manifest.get("src/main.tsx")
    if not isinstance(entry, dict):
        return ""
    safe = escape(assets_path)
    tags = ""
    # CSS chunks linked by the entry
    css_files = entry.get("css")
    if not isinstance(css_files, list):
        css_files = []
    for css_file in css_files:
        tags += f"<link rel='stylesheet' href='{safe}/{escape(str(css_file))}'>"
    # The JS entry itself
    js_file = entry.get("file")
    if js_file:
        tags += f"<script type='module' src='{safe}/{escape(str(js_file))}'></script>"
    return tags


def _shell(
    title: str,
    assets_path: str,
    body: str,
    *,
    extra_css: tuple[str, ...] = (),
    scripts: tuple[str, ...] = (),
    pre_vite_modules: tuple[str, ...] = (),
    xterm_cdn: str = "",
    fitaddon_cdn: str = "",
    fonts_cdn: str = "",
    xterm_cdn_integrity: str = "",
    fitaddon_cdn_integrity: str = "",
) -> str:
    vite_tags = _vite_entry_tags(assets_path)
    # When Vite manifest is available, the React app takes over rendering —
    # skip the legacy vanilla JS entry points.
    if vite_tags:
        scripts = ()
        extra_css = ()
    css_links = "".join(f"<link rel='stylesheet' href='{escape(assets_path)}/{escape(name)}'>" for name in extra_css)
    script_tags = "".join(
        f"<script type='module' src='{escape(assets_path)}/{escape(name)}'></script>" for name in scripts
    )
    # pre_vite_modules are type="module" scripts that must execute BEFORE the Vite
    # React bundle so their exports are available when React effects run.
    pre_vite_tags = "".join(
        f"<script type='module' src='{escape(assets_path)}/{escape(name)}'></script>" for name in pre_vite_modules
    )
    # SRI: when an integrity hash is provided, emit integrity= and
    # crossorigin=anonymous on the <script> tag so the browser refuses to
    # execute a tampered CDN asset.  This is the primary defense against a
    # compromised third-party CDN exfiltrating share/control tokens from the
    # DOM or making authenticated requests with the page's cookies.
    _xterm_sri = f" integrity='{escape(xterm_cdn_integrity)}' crossorigin='anonymous'" if xterm_cdn_integrity else ""
    _fitaddon_sri = (
        f" integrity='{escape(fitaddon_cdn_integrity)}' crossorigin='anonymous'" if fitaddon_cdn_integrity else ""
    )
    xterm_css = f"<link rel='stylesheet' href='{escape(xterm_cdn)}/css/xterm.css'>" if xterm_cdn else ""
    xterm_js = f"<script src='{escape(xterm_cdn)}/lib/xterm.js'{_xterm_sri}></script>" if xterm_cdn else ""
    fitaddon_js = (
        f"<script src='{escape(fitaddon_cdn)}/lib/addon-fit.js'{_fitaddon_sri}></script>" if fitaddon_cdn else ""
    )
    fonts_link = f"<link href='{escape(fonts_cdn)}' rel='stylesheet'>" if fonts_cdn else ""
    # When React app is active, skip legacy vanilla CSS files
    legacy_css = ""
    if not vite_tags:
        legacy_css = (
            f"<link rel='stylesheet' href='{escape(assets_path)}/server-app-foundation.css'>"
            f"<link rel='stylesheet' href='{escape(assets_path)}/server-app-layout.css'>"
            f"<link rel='stylesheet' href='{escape(assets_path)}/server-app-components.css'>"
            f"<link rel='stylesheet' href='{escape(assets_path)}/server-app-views.css'>"
        )
    return (
        "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
        f"<title>{escape(title)}</title>"
        f"{legacy_css}"
        f"{css_links}{xterm_css}{fonts_link}"
        f"{xterm_js}{fitaddon_js}"
        f"{pre_vite_tags}"
        f"{vite_tags}"
        f"{body}{script_tags}</html>"
    )


def _bootstrap_tag(payload: Mapping[str, object]) -> str:
    blob = json.dumps(payload).replace("</", "<\\/")
    return f"<script type='application/json' id='app-bootstrap'>{blob}</script>"


def operator_dashboard_html(
    title: str,
    app_path: str,
    assets_path: str,
    xterm_cdn: str = "",
    fitaddon_cdn: str = "",
    fonts_cdn: str = "",
    *,
    xterm_cdn_integrity: str = "",
    fitaddon_cdn_integrity: str = "",
) -> str:
    bootstrap = {
        "page_kind": "dashboard",
        "title": title,
        "app_path": app_path,
        "assets_path": assets_path,
    }
    body = (
        "<body>"
        "<div id='app-root'></div>"
        "<noscript><div class='page'><div class='card'>This application requires JavaScript.</div></div></noscript>"
        f"{_bootstrap_tag(bootstrap)}"
        "</body>"
    )
    return _shell(
        title,
        assets_path,
        body,
        xterm_cdn=xterm_cdn,
        fitaddon_cdn=fitaddon_cdn,
        fonts_cdn=fonts_cdn,
        xterm_cdn_integrity=xterm_cdn_integrity,
        fitaddon_cdn_integrity=fitaddon_cdn_integrity,
    )


def session_page_html(
    title: str,
    assets_path: str,
    session_id: str,
    *,
    operator: bool,
    app_path: str,
    share_role: str | None = None,
    xterm_cdn: str = "",
    fitaddon_cdn: str = "",
    fonts_cdn: str = "",
    xterm_cdn_integrity: str = "",
    fitaddon_cdn_integrity: str = "",
) -> str:
    bootstrap = {
        "page_kind": "operator" if operator else "session",
        "title": title,
        "app_path": app_path,
        "assets_path": assets_path,
        "session_id": session_id,
        "surface": "operator" if operator else "user",
        "share_role": share_role,
    }
    body = (
        "<body>"
        "<div id='app-root'></div>"
        "<noscript><div class='page'><div class='card'>This application requires JavaScript.</div></div></noscript>"
        f"{_bootstrap_tag(bootstrap)}"
        "</body>"
    )
    return _shell(
        title,
        assets_path,
        body,
        # hijack.js must appear before the Vite React bundle so window.ProvideHijack
        # is set before React's useEffect runs (both are deferred modules; document
        # order determines execution order).
        pre_vite_modules=(f"hijack.js?v={_hijack_js_version()}",),
        xterm_cdn=xterm_cdn,
        fitaddon_cdn=fitaddon_cdn,
        fonts_cdn=fonts_cdn,
        xterm_cdn_integrity=xterm_cdn_integrity,
        fitaddon_cdn_integrity=fitaddon_cdn_integrity,
    )


def connect_page_html(
    title: str,
    assets_path: str,
    app_path: str,
    *,
    xterm_cdn: str = "",
    fitaddon_cdn: str = "",
    fonts_cdn: str = "",
    xterm_cdn_integrity: str = "",
    fitaddon_cdn_integrity: str = "",
) -> str:
    """Return the quick-connect page, rendered by the frontend connect-view."""
    bootstrap = {
        "page_kind": "connect",
        "title": title,
        "app_path": app_path,
        "assets_path": assets_path,
    }
    body = (
        "<body>"
        "<div id='app-root'></div>"
        "<noscript><div class='page'><div class='card'>This application requires JavaScript.</div></div></noscript>"
        f"{_bootstrap_tag(bootstrap)}"
        "</body>"
    )
    return _shell(
        title,
        assets_path,
        body,
        xterm_cdn=xterm_cdn,
        fitaddon_cdn=fitaddon_cdn,
        fonts_cdn=fonts_cdn,
        xterm_cdn_integrity=xterm_cdn_integrity,
        fitaddon_cdn_integrity=fitaddon_cdn_integrity,
    )


def inspect_page_html(
    title: str,
    assets_path: str,
    session_id: str,
    *,
    app_path: str,
    share_role: str | None = None,
    xterm_cdn: str = "",
    fitaddon_cdn: str = "",
    fonts_cdn: str = "",
    xterm_cdn_integrity: str = "",
    fitaddon_cdn_integrity: str = "",
) -> str:
    bootstrap = {
        "page_kind": "inspect",
        "title": title,
        "app_path": app_path,
        "assets_path": assets_path,
        "session_id": session_id,
        "surface": "operator",
        "share_role": share_role,
    }
    body = (
        "<body>"
        "<div id='app-root'></div>"
        "<noscript><div class='page'><div class='card'>This application requires JavaScript.</div></div></noscript>"
        f"{_bootstrap_tag(bootstrap)}"
        "</body>"
    )
    return _shell(
        title,
        assets_path,
        body,
        xterm_cdn=xterm_cdn,
        fitaddon_cdn=fitaddon_cdn,
        fonts_cdn=fonts_cdn,
        xterm_cdn_integrity=xterm_cdn_integrity,
        fitaddon_cdn_integrity=fitaddon_cdn_integrity,
    )


def replay_page_html(
    title: str,
    assets_path: str,
    session_id: str,
    *,
    app_path: str,
    share_role: str | None = None,
    xterm_cdn: str = "",
    fitaddon_cdn: str = "",
    fonts_cdn: str = "",
    xterm_cdn_integrity: str = "",
    fitaddon_cdn_integrity: str = "",
) -> str:
    bootstrap = {
        "page_kind": "replay",
        "title": title,
        "app_path": app_path,
        "assets_path": assets_path,
        "session_id": session_id,
        "surface": "operator",
        "share_role": share_role,
    }
    body = (
        "<body>"
        "<div id='app-root'></div>"
        "<noscript><div class='page'><div class='card'>This application requires JavaScript.</div></div></noscript>"
        f"{_bootstrap_tag(bootstrap)}"
        "</body>"
    )
    return _shell(
        f"{title} Replay",
        assets_path,
        body,
        xterm_cdn=xterm_cdn,
        fitaddon_cdn=fitaddon_cdn,
        fonts_cdn=fonts_cdn,
        xterm_cdn_integrity=xterm_cdn_integrity,
        fitaddon_cdn_integrity=fitaddon_cdn_integrity,
    )
