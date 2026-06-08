#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Run workspace test suites in sequence (Python + frontend).

Each Python workspace package defines its own coverage gate in
``pyproject.toml``. The root ``uv run pytest`` only covers core + Cloudflare
(``[tool.pytest.ini_options].testpaths``), so this wrapper also runs the
remaining Python package suites. It then runs npm workspace typechecks/tests
for ``provide-uterm-frontend`` and ``provide-uterm-app`` so contributors have
one command that approximates CI scope across languages.

Exits non-zero on the first package whose tests fail, surfacing the raw pytest
output for that package. Pass through any extra args to every pytest invocation
(for example ``--no-cov -k name``).
"""

from __future__ import annotations

import shlex
import subprocess  # nosec
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

_PYTEST_SUITES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("provide-uterm + provide-uterm-cloudflare (root pytest)", (), ()),
    # Core unit coverage gate. The root run above executes core+cloudflare (incl.
    # the playwright browser suites) under the *root* config, which carries no
    # --cov. Re-run the core unit tests FROM the package dir (via uv --directory)
    # so pytest picks up packages/provide-uterm/pyproject.toml's
    # --cov-fail-under=100 gate; its --cov=src/provide/uterm path only resolves
    # with the package as CWD. (Its addopts deselect playwright/memray/slow,
    # which the root run still covers.)
    ("provide-uterm (core, coverage gate)", ("--directory", "packages/provide-uterm"), ("tests",)),
    ("provide-uterm-annotation", (), ("packages/provide-uterm-annotation/tests/",)),
    ("provide-uterm-server", (), ("packages/provide-uterm-server/tests/",)),
    ("provide-uterm-client", (), ("packages/provide-uterm-client/tests/",)),
    (
        "provide-uterm-platform/manager",
        (),
        ("packages/provide-uterm-platform/tests/manager/",),
    ),
    (
        "provide-uterm-platform/pty (no PAM/root)",
        ("--package", "provide-uterm-platform", "--extra", "dev"),
        (
            "packages/provide-uterm-platform/tests/pty/",
            "--ignore=packages/provide-uterm-platform/tests/pty/e2e",
            # --no-cov: the platform package's pyproject addopts apply --cov to
            # both provide.uterm.pty *and* provide.uterm.manager with
            # --cov-fail-under=100. Running pty tests alone can't satisfy the
            # manager coverage target — manager coverage is enforced separately
            # by the manager suite above. The CI pty-unit job mirrors this.
            "--no-cov",
            "--timeout=10",
            "-o",
            "addopts=--import-mode=importlib",
        ),
    ),
    (
        "conformance (FastAPI ↔ Cloudflare parity)",
        (),
        ("tests/conformance/", "-o", "addopts=--import-mode=importlib"),
    ),
)


def _run(label: str, uv_args: tuple[str, ...], pytest_args: tuple[str, ...], passthrough: list[str]) -> int:
    print(f"\n=== {label} ===", flush=True)
    cmd = ["uv", "run", *uv_args, "pytest", "-q", *pytest_args, *passthrough]
    return subprocess.call(cmd, cwd=str(_REPO_ROOT))


def _run_npm(label: str, command: str) -> int:
    print(f"\n=== {label} ===", flush=True)
    cmd = shlex.split(command)
    return subprocess.call(cmd, cwd=str(_REPO_ROOT))


def main() -> int:
    passthrough = sys.argv[1:]
    for label, uv_args, pytest_args in _PYTEST_SUITES:
        rc = _run(label, uv_args, pytest_args, passthrough)
        if rc != 0:
            print(f"FAILED: {label} (exit {rc})", file=sys.stderr)
            return rc
    for label, command in (
        ("provide-uterm-frontend typecheck", "npm run typecheck:frontend"),
        ("provide-uterm-app typecheck", "npm run typecheck:app"),
        ("provide-uterm-frontend tests", "npm test --workspace=packages/provide-uterm-frontend"),
        ("provide-uterm-app tests", "npm test --workspace=packages/provide-uterm-app"),
    ):
        rc = _run_npm(label, command)
        if rc != 0:
            print(f"FAILED: {label} (exit {rc})", file=sys.stderr)
            return rc
    print("\nAll package test suites passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
