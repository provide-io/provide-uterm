#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Frontend asset presence checks for the hosted terminal server."""

from __future__ import annotations

import importlib.resources


def _validate_frontend_assets() -> None:
    frontend_root = importlib.resources.files("provide.terminal.server") / "frontend"
    # Require the Vite manifest (React app) and standalone HTML pages.
    required = ("hijack.html", "terminal.html")
    missing = [name for name in required if not (frontend_root / name).is_file()]
    if not (frontend_root / ".vite" / "manifest.json").is_file():
        missing.append(".vite/manifest.json")
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"missing required frontend assets: {joined}")
