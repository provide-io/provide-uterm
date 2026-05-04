#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Static frontend asset structure tests."""

from __future__ import annotations

from pathlib import Path


def _frontend_path(name: str) -> Path:
    # Frontend assets live in provide-uterm-server after packaging restructure
    return (
        Path(__file__).resolve().parents[3]
        / "provide-uterm-server"
        / "src"
        / "provide"
        / "terminal"
        / "server"
        / "frontend"
        / name
    )


def test_terminal_html_uses_external_page_assets_only() -> None:
    html = _frontend_path("terminal.html").read_text(encoding="utf-8")

    assert "<style>" not in html
    assert "<script>" not in html
    assert "terminal-page.css" in html
    assert "terminal-page.js" in html
    assert "terminal-panel.js" not in html


def test_hijack_html_uses_external_page_assets_only() -> None:
    html = _frontend_path("hijack.html").read_text(encoding="utf-8")

    assert "<style>" not in html
    assert "<script>" not in html
    assert "hijack-page.css" in html
    assert "hijack-page.js" in html
    assert "window.demoHijack" not in html
