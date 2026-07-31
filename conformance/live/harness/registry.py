#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Which languages have a driver, in which role, and how to start it.

A language whose driver is missing is reported as missing, with the reason.
The failure this guards against is an incomplete matrix that reads like a
complete one: four green cells look the same as sixteen green cells in every
summary line ever written, so the count and the gaps are always printed.
"""

from __future__ import annotations

import shutil
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from harness.drivers import DriverSpec

#: The repository root, four levels up from this file.
REPO_ROOT: Final = Path(__file__).resolve().parents[3]

CLIENT: Final = "client"
SERVER: Final = "server"

#: The order languages are reported in: the reference first, then the ports.
LANGUAGES: Final = ("python", "go", "csharp", "typescript")


@dataclass(frozen=True)
class Registration:
    """One language's driver: where it is, and what it can be."""

    language: str
    roles: frozenset[str]
    build: Callable[[Path], DriverSpec]
    needs_files: tuple[str, ...]
    needs_tools: tuple[str, ...]
    note: str = ""

    def missing(self, root: Path) -> str | None:
        """Why this driver cannot run, or ``None`` when it can."""
        absent = [name for name in self.needs_files if not (root / name).exists()]
        if absent:
            return f"{self.language}: not built ({', '.join(absent)} missing)"
        unfound = [tool for tool in self.needs_tools if shutil.which(tool) is None]
        if unfound:
            return f"{self.language}: {', '.join(unfound)} not on PATH"
        return None


def _python(root: Path) -> DriverSpec:
    return DriverSpec(
        language="python",
        command=(sys.executable, str(root / "conformance/live/drivers/python/driver.py")),
        cwd=root,
        client_capabilities=("status.observed", "hijack.ws", "hijack.rest", "fanout.rest.strict"),
    )


def _typescript(root: Path) -> DriverSpec:
    return DriverSpec(
        language="typescript",
        command=("node", str(root / "packages/provide-uterm-ts/bin/uterm-conformance.mjs")),
        cwd=root,
        client_capabilities=("hijack.rest", "status.observed", "fanout.rest.strict"),
    )


def _go(root: Path) -> DriverSpec:
    """The Go driver, preferring a built binary over building it every cell.

    ``GOWORK=off`` because a contributor's repo-root ``go.work`` (untracked,
    local-only) can point at sibling modules that are not on this machine, and
    every ``go`` command in the module fails until it is ignored. The driver
    must build from the module itself in any case.
    """
    package = root / "packages/provide-uterm-go"
    built = package / "bin/uterm-live-driver"
    command = (str(built),) if built.exists() else ("go", "run", "./cmd/uterm-live-driver")
    return DriverSpec(
        language="go",
        command=command,
        cwd=package,
        env={"GOWORK": "off"},
        client_capabilities=(
            "hijack.rest",
            "sessions.rest",
            "http.raw",
            "auth.dev_token",
            "status.observed",
            "fanout.rest.strict",
        ),
    )


def _csharp(root: Path) -> DriverSpec:
    return DriverSpec(
        language="csharp",
        command=("dotnet", "run", "--project", "src/Provide.Uterm.LiveDriver", "-c", "Release", "--"),
        cwd=root / "packages/provide-uterm-csharp",
        client_capabilities=(
            "hijack.rest",
            "sessions.rest",
            "http.raw",
            "auth.dev_token",
            "status.observed",
            "fanout.rest.strict",
        ),
    )


#: Every driver this repository knows about.
REGISTRY: Final[tuple[Registration, ...]] = (
    Registration(
        language="python",
        roles=frozenset({CLIENT, SERVER}),
        build=_python,
        needs_files=("conformance/live/drivers/python/driver.py",),
        needs_tools=(),
        note="the reference implementation",
    ),
    Registration(
        language="go",
        roles=frozenset({CLIENT, SERVER}),
        build=_go,
        needs_files=("packages/provide-uterm-go/cmd/uterm-live-driver",),
        needs_tools=("go",),
    ),
    Registration(
        language="csharp",
        roles=frozenset({CLIENT, SERVER}),
        build=_csharp,
        needs_files=("packages/provide-uterm-csharp/src/Provide.Uterm.LiveDriver",),
        needs_tools=("dotnet",),
    ),
    Registration(
        language="typescript",
        roles=frozenset({CLIENT, SERVER}),
        build=_typescript,
        needs_files=("packages/provide-uterm-ts/bin/uterm-conformance.mjs",),
        needs_tools=("node",),
    ),
)


@dataclass(frozen=True)
class Available:
    """What can run, and why the rest cannot."""

    servers: tuple[DriverSpec, ...]
    clients: tuple[DriverSpec, ...]
    gaps: tuple[str, ...]

    @property
    def cell_count(self) -> int:
        return len(self.servers) * len(self.clients)


def available(
    root: Path = REPO_ROOT,
    *,
    only: Iterable[str] | None = None,
    registry: Sequence[Registration] = REGISTRY,
) -> Available:
    """The drivers that can run right now, and a line for each that cannot."""
    wanted = set(only) if only is not None else None
    servers: list[DriverSpec] = []
    clients: list[DriverSpec] = []
    gaps: list[str] = []
    for registration in registry:
        if wanted is not None and registration.language not in wanted:
            continue
        reason = registration.missing(root)
        if reason is not None:
            gaps.append(reason)
            continue
        spec = registration.build(root)
        if SERVER in registration.roles:
            servers.append(spec)
        else:
            gaps.append(f"{registration.language}: no server role ({registration.note})")
        if CLIENT in registration.roles:
            clients.append(spec)
    return Available(tuple(servers), tuple(clients), tuple(gaps))
