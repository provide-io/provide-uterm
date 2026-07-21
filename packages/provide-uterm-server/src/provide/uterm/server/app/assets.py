#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Frontend asset presence checks for the hosted terminal server."""

from __future__ import annotations

import importlib.resources


def _has_vite_manifest(frontend_root: object) -> bool:
    """True if a Vite app manifest is present (dot or package-data-safe path)."""
    # Prefer package-data-safe vite-manifest.json (setuptools omits .vite/).
    if (frontend_root / "vite-manifest.json").is_file():  # type: ignore[operator]
        return True
    return (frontend_root / ".vite" / "manifest.json").is_file()  # type: ignore[operator]


def _validate_frontend_assets() -> None:
    frontend_root = importlib.resources.files("provide.uterm.server") / "frontend"
    # Require the Vite manifest (React app) and standalone HTML pages.
    required = ("hijack.html", "terminal.html")
    missing = [name for name in required if not (frontend_root / name).is_file()]
    if not _has_vite_manifest(frontend_root):
        missing.append("vite-manifest.json (or .vite/manifest.json)")
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"missing required frontend assets: {joined}")
