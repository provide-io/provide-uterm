#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Structural: baked first-party VNC console assets exist (no Docker)."""

from __future__ import annotations

import importlib.resources
import json
from pathlib import Path


def _frontend_root() -> Path:
    return Path(str(importlib.resources.files("provide.uterm.server") / "frontend"))


def test_vnc_html_baked() -> None:
    root = _frontend_root()
    vnc = root / "vnc.html"
    assert vnc.is_file(), f"missing baked vnc.html under {root}"
    text = vnc.read_text(encoding="utf-8")
    assert "vnc-screen" in text
    assert "vnc-status" in text
    # Built page must load a module script (Vite entry), not a hand-rolled RFB.
    assert 'type="module"' in text or "type='module'" in text


def test_vnc_manifest_entry() -> None:
    # Vanilla (xterm/hijack/vnc) pages write vanilla-manifest.json; the React SPA
    # uses vite-manifest.json separately.
    root = _frontend_root()
    path = root / "vanilla-manifest.json"
    assert path.is_file(), f"missing {path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    keys = set(data.keys())
    assert any("vnc" in k for k in keys), f"no vnc entry in vanilla-manifest: {sorted(keys)[:30]}"


def test_novnc_rfb_chunk_present() -> None:
    """Real noVNC client must be in the bake (not a zero-byte stub)."""
    root = _frontend_root()
    assets = root / "assets"
    assert assets.is_dir()
    # Vite names the chunk rfb-*.js when dynamic-importing @novnc/novnc/lib/rfb.js
    rfb_chunks = list(assets.glob("rfb-*.js"))
    assert rfb_chunks, f"expected rfb-*.js under {assets}"
    assert rfb_chunks[0].stat().st_size > 50_000


def test_source_vnc_page_exists_in_frontend_package() -> None:
    # Repo-relative structural check from the server package tests.
    repo = Path(__file__).resolve().parents[4]
    page = repo / "packages" / "provide-uterm-frontend" / "vnc.html"
    ts = repo / "packages" / "provide-uterm-frontend" / "src" / "vnc-page.ts"
    url_ts = repo / "packages" / "provide-uterm-frontend" / "src" / "vnc-url.ts"
    assert page.is_file()
    assert ts.is_file()
    assert url_ts.is_file()
    src = ts.read_text(encoding="utf-8")
    url_src = url_ts.read_text(encoding="utf-8")
    assert "@novnc/novnc/lib/rfb.js" in src
    assert "gui/vnc" in url_src
