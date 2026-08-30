#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared package metadata used by release and artifact verification scripts."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PackageSpec:
    name: str
    import_names: tuple[str, ...]
    required_members: tuple[str, ...]
    entry_points: dict[str, str]
    # Extras a bare install does NOT pull in but `import_names` needs anyway.
    # Verification installs the package and imports every name in that tuple, so
    # anything whose dependency lives behind an extra fails on import unless the
    # extra is requested. provide-uterm-platform imports provide.uterm.manager,
    # whose fastapi is in the `manager` extra, and the 0.5.1 release died on
    # exactly that: "ModuleNotFoundError: No module named 'fastapi'".
    install_extras: tuple[str, ...] = field(default_factory=tuple)

    @property
    def wheel_prefix(self) -> str:
        return self.name.replace("-", "_")

    @property
    def install_spec(self) -> str:
        """``name[extra,extra]`` for pip, or bare ``name`` when none are needed."""
        if not self.install_extras:
            return self.name
        return f"{self.name}[{','.join(self.install_extras)}]"


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
        # provide.uterm.manager imports fastapi eagerly, and fastapi is in the
        # `manager` extra. provide.uterm.pty needs nothing beyond the base.
        install_extras=("manager",),
    ),
    PackageSpec(
        name="provide-uterm-cloudflare",
        import_names=("provide.uterm.cloudflare",),
        required_members=("provide/uterm/cloudflare/py.typed",),
        entry_points={"uterm-cf": "provide.uterm.cloudflare.cli:main"},
    ),
    PackageSpec(
        name="provide-uterm-annotation",
        import_names=("provide.uterm.annotation",),
        required_members=("provide/uterm/annotation/py.typed",),
        # No console scripts: it is a library layer, not a tool.
        entry_points={},
    ),
)


DEPENDENT_PACKAGES: tuple[str, ...] = (
    "provide-uterm-server",
    "provide-uterm-client",
    "provide-uterm-platform",
    "provide-uterm-annotation",
)


def _main(argv: list[str]) -> int:
    """Print the pip install spec for a package, so shell callers share this table.

    ci/install_from_testpypi.sh needs the extras, and a second copy of them in
    shell would be a second thing to forget to update. ``--names`` prints every
    published name instead, which the same script uses to pre-fetch our own
    distributions from TestPyPI before resolving anything against PyPI.
    """
    if len(argv) == 2 and argv[1] == "--names":
        sys.stdout.write("\n".join(package.name for package in PUBLISHED_PACKAGES) + "\n")
        return 0
    if len(argv) != 2:
        sys.stderr.write("usage: package_metadata.py <package> | --names\n")
        return 2
    by_name = {package.name: package for package in PUBLISHED_PACKAGES}
    package = by_name.get(argv[1])
    if package is None:
        sys.stderr.write(f"unknown package: {argv[1]}\n")
        return 2
    sys.stdout.write(package.install_spec + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
