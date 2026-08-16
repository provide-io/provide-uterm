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

Every leg runs, whatever the ones before it did, and a summary at the end names
each leg's verdict. Stopping at the first failure meant one broken package hid
every suite after it -- which is not a hypothetical: the last leg had never
executed at all, and two real defects sat behind earlier failures. Exits
non-zero if any leg failed, surfacing the raw output for each.

Pass ``--fail-fast`` to stop at the first failure instead, for iterating on a
single package without paying for the whole sweep; the summary then reports how
many legs went unrun, so a short run is never mistaken for a clean one. Any
other extra args pass through to every pytest invocation (for example
``--no-cov -k name``).
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
    # `-m "not go_interop"` mirrors what CI's client-quality job does
    # (.github/workflows/ci.yml). The go_interop test builds and runs a real Go
    # `uterm server` binary, so CI gives it a job of its own where the Go
    # toolchain is set up; running it here made a clean local tree look broken —
    # an untracked `go.work` naming an absent sibling module fails `go build`
    # before the test can start. That used to take every later suite down with
    # it; legs are independent now, but the exclusion still belongs here,
    # because a local failure this runner cannot act on is just noise.
    ("provide-uterm-client", (), ("packages/provide-uterm-client/tests/", "-m", "not go_interop")),
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
    # Tests for the repo's own tooling. tests/scripts/ is outside the root
    # testpaths and was named by no gate at all, so the suite in it had never
    # run anywhere — the same way tests/conformance/ went unenforced in CI.
    (
        "scripts (repo tooling)",
        (),
        ("tests/scripts/", "-o", "addopts=--import-mode=importlib"),
    ),
)


_NPM_SUITES: tuple[tuple[str, str], ...] = (
    ("provide-uterm-frontend typecheck", "npm run typecheck:frontend"),
    ("provide-uterm-app typecheck", "npm run typecheck:app"),
    ("provide-uterm-frontend tests", "npm test --workspace=packages/provide-uterm-frontend"),
    ("provide-uterm-app tests", "npm test --workspace=packages/provide-uterm-app"),
    ("browser consumer contract", "npm run test:browser-consumer"),
)

_FAIL_FAST = "--fail-fast"


def _finished(label: str, rc: int) -> int:
    """Announce a leg's exit code next to its own output, and return it."""
    if rc != 0:
        print(f"FAILED: {label} (exit {rc})", file=sys.stderr)
    return rc


def _run(label: str, uv_args: tuple[str, ...], pytest_args: tuple[str, ...], passthrough: list[str]) -> int:
    print(f"\n=== {label} ===", flush=True)
    cmd = ["uv", "run", *uv_args, "pytest", "-q", *pytest_args, *passthrough]
    return _finished(label, subprocess.call(cmd, cwd=str(_REPO_ROOT)))


def _run_npm(label: str, command: str) -> int:
    print(f"\n=== {label} ===", flush=True)
    cmd = shlex.split(command)
    return _finished(label, subprocess.call(cmd, cwd=str(_REPO_ROOT)))


def _summarize(results: list[tuple[str, int]], total: int) -> int:
    """Print every leg's verdict and return the process exit code.

    A leg that never ran is listed too. Without that line a --fail-fast run
    looks exactly like a full one in the scrollback, which is how the last leg
    went unnoticed for so long.
    """
    print("\n=== summary ===", flush=True)
    for label, rc in results:
        print(f"  {'PASS' if rc == 0 else 'FAIL':<7} {label}" + ("" if rc == 0 else f" (exit {rc})"))
    unrun = total - len(results)
    if unrun:
        print(f"  {'NOT RUN':<7} {unrun} leg(s), stopped early by {_FAIL_FAST}")
    failed = [label for label, rc in results if rc != 0]
    if failed or unrun:
        print(f"\n{len(failed)} of {total} leg(s) FAILED: {', '.join(failed)}", file=sys.stderr)
        return 1
    print("\nAll package test suites passed.")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    fail_fast = _FAIL_FAST in argv
    passthrough = [arg for arg in argv if arg != _FAIL_FAST]
    total = len(_PYTEST_SUITES) + len(_NPM_SUITES)
    results: list[tuple[str, int]] = []

    for label, uv_args, pytest_args in _PYTEST_SUITES:
        results.append((label, _run(label, uv_args, pytest_args, passthrough)))
        if fail_fast and results[-1][1] != 0:
            return _summarize(results, total)

    for label, command in _NPM_SUITES:
        results.append((label, _run_npm(label, command)))
        if fail_fast and results[-1][1] != 0:
            return _summarize(results, total)

    return _summarize(results, total)


if __name__ == "__main__":
    raise SystemExit(main())
