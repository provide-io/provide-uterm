#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""HTML helpers for the hosted server UI surfaces."""

from __future__ import annotations

import json
from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def _shell(
    title: str,
    assets_path: str,
    body: str,
    *,
    extra_css: tuple[str, ...] = (),
    scripts: tuple[str, ...] = (),
    xterm_cdn: str = "",
    fonts_cdn: str = "",
) -> str:
    css_links = "".join(f"<link rel='stylesheet' href='{escape(assets_path)}/{escape(name)}'>" for name in extra_css)
    script_tags = "".join(
        f"<script type='module' src='{escape(assets_path)}/{escape(name)}'></script>" for name in scripts
    )
    xterm_css = f"<link rel='stylesheet' href='{escape(xterm_cdn)}/css/xterm.css'>" if xterm_cdn else ""
    fonts_link = f"<link href='{escape(fonts_cdn)}' rel='stylesheet'>" if fonts_cdn else ""
    return (
        "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
        f"<title>{escape(title)}</title>"
        f"<link rel='stylesheet' href='{escape(assets_path)}/server-app-foundation.css'>"
        f"<link rel='stylesheet' href='{escape(assets_path)}/server-app-layout.css'>"
        f"<link rel='stylesheet' href='{escape(assets_path)}/server-app-components.css'>"
        f"<link rel='stylesheet' href='{escape(assets_path)}/server-app-views.css'>"
        f"{css_links}{xterm_css}{fonts_link}"
        f"{body}{script_tags}</html>"
    )


def _bootstrap_tag(payload: Mapping[str, object]) -> str:
    blob = json.dumps(payload).replace("</", "<\\/")
    return f"<script type='application/json' id='app-bootstrap'>{blob}</script>"


def operator_dashboard_html(
    title: str, app_path: str, assets_path: str, xterm_cdn: str = "", fonts_cdn: str = ""
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
        title, assets_path, body, scripts=("server-session-page.js",), xterm_cdn=xterm_cdn, fonts_cdn=fonts_cdn
    )


def session_page_html(
    title: str,
    assets_path: str,
    session_id: str,
    *,
    operator: bool,
    app_path: str,
    xterm_cdn: str = "",
    fonts_cdn: str = "",
) -> str:
    bootstrap = {
        "page_kind": "operator" if operator else "session",
        "title": title,
        "app_path": app_path,
        "assets_path": assets_path,
        "session_id": session_id,
        "surface": "operator" if operator else "user",
    }
    body = (
        "<body>"
        "<div id='app-root'></div>"
        "<noscript><div class='page'><div class='card'>This application requires JavaScript.</div></div></noscript>"
        f"{_bootstrap_tag(bootstrap)}"
        f"<script src='{escape(assets_path)}/hijack.js'></script>"
        "</body>"
    )
    return _shell(
        title, assets_path, body, scripts=("server-session-page.js",), xterm_cdn=xterm_cdn, fonts_cdn=fonts_cdn
    )


def replay_page_html(
    title: str, assets_path: str, session_id: str, *, app_path: str, xterm_cdn: str = "", fonts_cdn: str = ""
) -> str:
    bootstrap = {
        "page_kind": "replay",
        "title": title,
        "app_path": app_path,
        "assets_path": assets_path,
        "session_id": session_id,
        "surface": "operator",
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
        scripts=("server-replay-page.js",),
        xterm_cdn=xterm_cdn,
        fonts_cdn=fonts_cdn,
    )
