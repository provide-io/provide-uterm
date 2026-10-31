#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""What a change makes the Python mutation gate run, decided path by path, deny by default.

Every changed path gets exactly one kind:

``support``
    A file that defines what the gate runs or how its verdict is reached, a
    file the mutmut/pytest bootstrap loads, or a bound mutation test
    (``MUTATION_SUPPORT_FILES`` and the test selection). It runs the whole
    perimeter.
``source``
    A perimeter source file. It runs its own target.
``irrelevant``
    A path on the short, commented allowlist below, which no Python test can
    import or read. It runs nothing.
``python-side``
    Everything else -- off-perimeter source, tests, conftests, helpers, test
    data, corpora, packaging and dependency changes, anything new. It runs the
    whole perimeter.

The last kind is the point. An earlier version of this file narrowed tests,
helpers and off-perimeter modules to the targets a static reading said they
reach, and each review found another way the reading leaked: conftest parts
loaded by file path, a package ``__init__`` that runs before every module in
it, an import name built at run time. Every leak was an empty target list,
which passes. Narrowing is a planned follow-up; until then, anything this
file cannot place costs a full, sharded run instead of a silent pass, and the
allowlist is the only way to say "this cannot matter".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

KINDS = ("support", "source", "irrelevant", "python-side")


@dataclass(frozen=True)
class Irrelevant:
    """One allowlist entry: a file, or every path under a directory (ending ``/``)."""

    prefix: str
    reason: str
    #: Only paths ending in one of these, when given.
    suffixes: tuple[str, ...] = ()
    #: Paths under the prefix that the entry does not cover.
    excluded: tuple[str, ...] = ()
    excluded_suffixes: tuple[str, ...] = ()

    def matches(self, path: str) -> bool:
        if not self.prefix.endswith("/"):
            return path == self.prefix
        if not path.startswith(self.prefix) or path in self.excluded:
            return False
        if self.excluded_suffixes and path.endswith(self.excluded_suffixes):
            return False
        return not self.suffixes or path.endswith(self.suffixes)


#: Paths no Python test can import or read. Every entry has to survive "when
#: unsure, it is NOT irrelevant": add one only with a reason a reviewer can
#: check against the tests, conftests and helpers.
IRRELEVANT: tuple[Irrelevant, ...] = (
    Irrelevant(
        "packages/provide-uterm-go/",
        "The Go port. It is built and gated by its own toolchain, and no Python test imports it. The one file a"
        " Python test reads, the capture-record corpus both ports check against, is excluded.",
        excluded=("packages/provide-uterm-go/capturestate/testdata/capture_record_golden.json",),
    ),
    Irrelevant(
        "packages/provide-uterm-ts/",
        "The TypeScript port. StrykerJS gates it, and no Python test imports or reads it.",
    ),
    Irrelevant(
        "packages/provide-uterm-csharp/",
        "The C# port. Stryker.NET gates it, and no Python test imports or reads it.",
    ),
    Irrelevant(
        "packages/provide-uterm-cloudflare/",
        "The Worker's JavaScript, TypeScript and config. Its Python (Pyodide) code and TOML are excluded.",
        excluded_suffixes=(".py", ".pyi", ".toml"),
    ),
    Irrelevant(
        "packages/provide-uterm-app/",
        "The standalone React app. The Python server neither serves nor reads it.",
    ),
    Irrelevant(
        "packages/provide-uterm-frontend/src/",
        "Frontend TypeScript and styles. Python tests need the built bundle's entry points to exist, and those "
        "come from the HTML files and vite config, which are not on this entry.",
        suffixes=(".ts", ".tsx", ".css"),
    ),
    Irrelevant(
        "docs/",
        "Prose. No test, conftest or helper reads the Markdown under docs/.",
        suffixes=(".md",),
    ),
    Irrelevant(
        ".github/",
        "Workflows and settings for other jobs. The gate's own workflow files are support files, which are "
        "classified before this list is read.",
    ),
    Irrelevant(
        "docker/",
        "Container images for the ports and the server, which mutmut does not run in. The server configs "
        "(*.toml) are excluded: the bound test_config.py loads docker/server.toml.",
        excluded_suffixes=(".toml",),
    ),
    Irrelevant(
        "scripts/",
        "Repo tooling. The repo root is off sys.path in a test run (root conftest.py), so no test imports "
        "it. The gate's own scripts are support files, classified first, and example_server.py, which the "
        "conftest parts load by file path, is excluded.",
        excluded=("scripts/example_server.py",),
    ),
    Irrelevant(
        "LICENSES/",
        "License texts for REUSE compliance. No test, conftest or helper reads them.",
    ),
    *(
        Irrelevant(name, reason)
        for name, reason in (
            ("README.md", "Top-level prose for people. No test, conftest or helper reads it."),
            ("CHANGELOG.md", "Top-level prose, the release notes. No test, conftest or helper reads it."),
            ("CLAUDE.md", "Top-level prose, agent guidance. No test, conftest or helper reads it."),
            ("AGENTS.md", "Top-level prose, agent guidance. No test, conftest or helper reads it."),
            ("SECURITY.md", "Top-level prose, the security policy. No test, conftest or helper reads it."),
            (
                "MUTATION_PATTERNS.md",
                "Top-level prose, mutation-testing patterns. No test, conftest or helper reads it.",
            ),
            ("package.json", "The npm workspace manifest, for the frontend and the ports. No Python test reads it."),
            ("package-lock.json", "The npm lockfile, for the frontend and the ports. No Python test reads it."),
            ("biome.json", "The TypeScript linter's configuration file. No Python test, conftest or helper reads it."),
            (".secrets.baseline", "The secret scanner's baseline file. No Python test, conftest or helper reads it."),
            (".pre-commit-config.yaml", "Local commit hooks for developers. Neither mutmut nor pytest reads it."),
            ("Makefile", "Developer shortcuts. CI runs the gate through its own scripts, not make."),
            ("Makefile.bench", "Benchmark shortcuts for developers. Neither mutmut nor pytest reads it."),
        )
    ),
)

#: Packages whose Python code, tests or data the perimeter's tests can import
#: or read. A path here is never allowlisted; TestTheAllowlist pins that.
_PYTHON_SIDE = (
    "packages/provide-uterm/",
    "packages/provide-uterm-server/",
    "packages/provide-uterm-platform/",
    "packages/provide-uterm-client/",
    "packages/provide-uterm-annotation/",
    "src/",
)


def is_python_side(path: str) -> bool:
    """Whether *path* belongs to a package the perimeter's tests can import or read, or is top-level Python."""
    return path.startswith(_PYTHON_SIDE) or (path.endswith((".py", ".pyi")) and "/" not in path)


def irrelevant_entry(path: str) -> Irrelevant | None:
    """The allowlist entry covering *path*, or None. Python-side paths are never covered."""
    if is_python_side(path):
        return None
    return next((entry for entry in IRRELEVANT if entry.matches(path)), None)


@dataclass(frozen=True)
class Classified:
    path: str
    kind: str
    reason: str
    #: The perimeter target, for a ``source`` path.
    target: str | None = None


def classify(path: str, *, is_support: Callable[[str], bool], source_target: Callable[[str], str | None]) -> Classified:
    """The kind of one changed path. There is no "unclassified": the fallback runs the perimeter."""
    if is_support(path):
        return Classified(path, "support", "defines what the gate runs or how it passes")
    target = source_target(path)
    if target is not None:
        return Classified(path, "source", f"perimeter source for {target}", target)
    entry = irrelevant_entry(path)
    if entry is not None:
        return Classified(path, "irrelevant", entry.reason)
    return Classified(path, "python-side", "not provably irrelevant, so it may affect any target")


@dataclass
class Scope:
    """What a change runs: targets to add, and the paths (with why) that run the whole perimeter."""

    targets: list[str] = field(default_factory=list)
    full: list[tuple[str, str]] = field(default_factory=list)
    classified: list[Classified] = field(default_factory=list)


def change_scope(
    changed: list[str], *, is_support: Callable[[str], bool], source_target: Callable[[str], str | None]
) -> Scope:
    """Classify every changed path and collect what the change runs."""
    result = Scope(classified=[classify(p, is_support=is_support, source_target=source_target) for p in changed])
    result.targets = sorted({c.target for c in result.classified if c.kind == "source" and c.target is not None})
    result.full = [(c.path, c.reason) for c in result.classified if c.kind in {"support", "python-side"}]
    return result


def gate_blocks(text: str | None, jobs: tuple[str, ...]) -> dict[str, tuple[str, ...]] | None:
    """The parts of ci.yml that shape the Python gate, minus comments and blank lines, or None if unreadable.

    Those are the given job blocks and every top-level key but ``jobs:`` --
    ``on:``, ``permissions:``, ``concurrency:``, ``env:``, ``defaults:`` --
    because a workflow-level key applies to the gate jobs too: an ``on:`` edit
    can stop the gate running on pull requests at all. Read as text, not YAML,
    because the targets job runs on a bare interpreter with no PyYAML. A block
    is its key line plus every following line indented further, which is what
    a key is in this workflow's two-space layout. Comment and blank lines are
    dropped so rewording an explanation is not a change to what the gate runs;
    any other edit is.
    """
    if text is None:
        return None
    lines = text.splitlines()

    def block(start: int, indent: str) -> tuple[str, ...]:
        body: list[str] = [lines[start].rstrip()]
        for line in lines[start + 1 :]:
            if line.strip() and not line.startswith(indent + " ") and not line.strip().startswith("#"):
                break
            if line.strip() and not line.strip().startswith("#"):
                body.append(line.rstrip())
        return tuple(body)

    blocks: dict[str, tuple[str, ...]] = {}
    for index, line in enumerate(lines):
        if line and not line[0].isspace() and not line.startswith("#") and ":" in line:
            key = line.split(":", 1)[0].strip().strip("'\"")
            if key not in {"jobs", "name"}:
                blocks[f"top-level {key}"] = block(index, "")
    for job in jobs:
        start = next((i for i, line in enumerate(lines) if line.rstrip() == f"  {job}:"), None)
        if start is not None:
            blocks[job] = block(start, "  ")[1:]
    return blocks
