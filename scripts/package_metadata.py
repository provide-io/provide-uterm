#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared package metadata used by release and artifact verification scripts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PackageSpec:
    name: str
    import_names: tuple[str, ...]
    required_members: tuple[str, ...]
    entry_points: dict[str, str]

    @property
    def wheel_prefix(self) -> str:
        return self.name.replace("-", "_")


PUBLISHED_PACKAGES: tuple[PackageSpec, ...] = (
    PackageSpec(
        name="provide-uterm",
        import_names=("provide.uterm",),
        required_members=("provide/uterm/py.typed",),
        entry_points={},
    ),
    PackageSpec(
        name="provide-uterm-server",
        import_names=("provide.uterm.server", "provide.uterm.tunnel", "provide.uterm.gateway"),
        required_members=("provide/uterm/py.typed", "provide/uterm/server/frontend/"),
        entry_points={"uterm": "provide.uterm.cli:main"},
    ),
    PackageSpec(
        name="provide-uterm-client",
        import_names=("provide.uterm.client", "provide.uterm.transports", "provide.uterm.ai"),
        required_members=("provide/uterm/ai/py.typed",),
        entry_points={"uterm-mcp": "provide.uterm.ai.cli:main"},
    ),
    PackageSpec(
        name="provide-uterm-platform",
        import_names=("provide.uterm.pty", "provide.uterm.manager"),
        required_members=("provide/uterm/py.typed", "provide/uterm/pty/py.typed"),
        entry_points={"uterm-manager": "provide.uterm.manager.cli:main"},
    ),
    PackageSpec(
        name="provide-uterm-cloudflare",
        import_names=("provide.uterm.cloudflare",),
        required_members=("provide/uterm/cloudflare/py.typed",),
        entry_points={"uterm-cf": "provide.uterm.cloudflare.cli:main"},
    ),
)


DEPENDENT_PACKAGES: tuple[str, ...] = (
    "provide-uterm-server",
    "provide-uterm-client",
    "provide-uterm-platform",
)
