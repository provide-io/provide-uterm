#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Cloudflare Worker entrypoint shim (referenced by ``main`` in wrangler.toml).

Anchored at the package ``src/`` root so wrangler bundles the full
``provide/uterm/cloudflare/`` tree (wrangler bundles ``main``'s directory),
letting the worker's canonical ``from provide.uterm.cloudflare.X import Y``
imports resolve as-is. Re-exports the ``Default`` HTTP handler and the
``SessionRuntime`` Durable Object for Cloudflare's Pyodide validation phase.
"""

from __future__ import annotations

from provide.uterm.cloudflare.entry import Default, SessionRuntime

__all__ = ["Default", "SessionRuntime"]
