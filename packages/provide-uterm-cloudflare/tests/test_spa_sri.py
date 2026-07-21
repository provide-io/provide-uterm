#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""M4: CF Worker SPA shell must ship SRI for CDN xterm CSS."""

from __future__ import annotations

from provide.uterm.cloudflare.entry.spa import _spa_response


def test_spa_response_includes_xterm_css_sri() -> None:
    resp = _spa_response("dashboard")
    body = resp.body if isinstance(resp.body, str) else resp.body.decode("utf-8")  # type: ignore[union-attr]
    assert "integrity=" in body
    assert "crossorigin='anonymous'" in body
    assert "xterm.css" in body
    assert "sha384-" in body


def test_spa_response_scripts_use_crossorigin() -> None:
    resp = _spa_response("session", session_id="s1")
    body = resp.body if isinstance(resp.body, str) else resp.body.decode("utf-8")  # type: ignore[union-attr]
    assert "xterm.js" in body
    assert "crossorigin='anonymous'" in body
