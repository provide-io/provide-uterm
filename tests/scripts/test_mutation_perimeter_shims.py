#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A perimeter entry that is a re-export shim must not stand in for the real module.

``[tool.mutmut].source_paths`` is a list of file paths, and a reader checking
whether a service is mutation-tested finds its name there and stops. Nothing
distinguishes a path that holds the code from one that holds three lines of
``from ... import X``.

That is not hypothetical. ``bridge/hub/router.py`` was on the perimeter for the
whole of 2026 while being exactly this:

    from provide.uterm.server.bridge.hub.router_impl import MessageRouter

    __all__ = ["MessageRouter"]

The router had been split for the 777-LOC limit and the documented rule for
that -- "the extracted sibling module is added to ``source_paths`` so its
mutants stay enforced" -- was not applied. So the perimeter listed the router
service, CLAUDE.md described the router service as enforced, and 1402 lines of
router had never had a single mutant generated against them. It surfaced only
because a fix touched ``router_broadcast.py`` and the changed-only gate selected
nothing (``docs/mutmut-survivors-triage.md`` Wave 10).

A shim on the perimeter is fine -- it costs nothing and keeps the import path
documented. Listing it *instead of* what it re-exports from is the failure. So:
for every perimeter entry that is a pure re-export, the modules it re-exports
from must be on the perimeter too.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: Known gaps: modules a perimeter shim re-exports from that are not themselves
#: enforced yet. Listed rather than tolerated silently — the whole point of the
#: check is that an unenforced module must not be invisible. Keyed by module
#: name, not path, because the perimeter lists some entries through the root
#: ``src/`` symlink tree and others by their real package path.
#:
#: ``app/factory.py`` is the same failure as ``bridge/hub/router.py``, and this
#: guard found it on the commit that introduced the guard: the FastAPI
#: application factory was split for the 777-LOC limit, the 11-line shim stayed
#: on the perimeter and the 610 lines of ``factory_impl.py`` were never added.
#:
#: **The kill-suites for it now exist and leave zero survivors** — the four
#: ``tests/server/test_factory_*_kill.py`` files, wired into
#: ``pytest_add_cli_args_test_selection`` and taking the file from 132 killed of
#: 545 to 415+ with ``survived: 0`` and ``suspicious: 0``. The path is still not
#: on the perimeter for one reason: **130 of the 545 mutants crash the test
#: process**, which mutmut records as ``segfault``. That state is not in
#: ``BAD_MUTANT_STATES``, so the gate reports ``bad_total: 0`` and still fails,
#: because the score is ``killed / total`` and the crashes sit in the
#: denominator — 76%, with nothing actionable in the survivor list.
#:
#: The crash needs the full ~4000-test covering selection to reproduce; the four
#: suites alone run clean under every mutant sampled. Until that is understood,
#: adding the path would only turn the advisory full-perimeter run red, which is
#: the mistake the original Wave 10 entry made in the other direction. Remove
#: this entry when the crashes are resolved, not before.
_KNOWN_UNENFORCED: frozenset[str] = frozenset({"provide.uterm.server.app.factory_impl"})

#: A module is a re-export shim when its body is nothing but imports, ``__all__``,
#: docstrings and ``from __future__`` — i.e. it defines no behaviour of its own.
_INERT_NODES = (ast.Import, ast.ImportFrom, ast.Expr, ast.Pass)


def _perimeter_paths() -> list[str]:
    config = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return [str(entry) for entry in config["tool"]["mutmut"]["source_paths"]]


def _is_reexport_shim(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, _INERT_NODES):
            continue
        # `__all__ = [...]` is bookkeeping, not behaviour.
        if isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            continue
        return False
    return True


def _reexport_sources(tree: ast.Module) -> set[str]:
    """The dotted module names this shim re-exports names from."""
    return {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}


def _module_name(path: Path) -> str | None:
    """The dotted import name for a repo path, via its ``src`` root."""
    parts = path.with_suffix("").parts
    if "src" not in parts:
        return None
    return ".".join(parts[parts.index("src") + 1 :])


def _perimeter_gaps(entries: list[str]) -> list[str]:
    """Shim-to-unenforced-module gaps across *entries*, in perimeter path form."""
    covered: set[str] = set()
    shims: list[tuple[str, ast.Module]] = []

    for entry in entries:
        path = REPOSITORY_ROOT / entry
        if not path.exists():  # pragma: no cover — a missing path is a different check's job
            continue
        # A perimeter entry may be a package directory (``routes/``); every
        # module under it is enforced, so every module under it counts as
        # covered and is checked for being a shim.
        for module_path in sorted(path.rglob("*.py")) if path.is_dir() else [path]:
            tree = ast.parse(module_path.read_text(encoding="utf-8"))
            name = _module_name(module_path)
            if name is not None:
                covered.add(name)
                # A package's ``__init__`` is importable under the package name too.
                if name.endswith(".__init__"):
                    covered.add(name.removesuffix(".__init__"))
            if _is_reexport_shim(tree):
                shims.append((str(module_path.relative_to(REPOSITORY_ROOT)), tree))

    gaps: list[str] = []
    for entry, tree in shims:
        for source in sorted(_reexport_sources(tree)):
            # Only in-repo modules can be put on the perimeter; a stdlib or
            # third-party re-export is out of scope by definition.
            if not source.startswith("provide.uterm"):
                continue
            if source in covered or source in _KNOWN_UNENFORCED:
                continue
            gaps.append(f"{entry} re-exports from {source}, which is NOT on the mutation perimeter")
    return gaps


def test_a_shim_on_the_perimeter_names_a_module_that_is_also_on_it() -> None:
    """The check that would have caught Wave 10 on the commit that introduced it."""
    gaps = _perimeter_gaps(_perimeter_paths())

    assert not gaps, (
        "A perimeter entry that is a re-export shim is indistinguishable from real coverage "
        "in the path list. Add the module holding the code, not just the shim:\n  " + "\n  ".join(gaps)
    )


def test_the_check_reports_a_shim_whose_module_is_missing() -> None:
    """The guard must fail on the state it exists to catch, or it proves nothing.

    ``router.py`` without ``router_impl.py`` is the exact configuration this
    repository shipped for the whole of 2026. Reconstructed here by dropping the
    impl from the perimeter, so a future refactor that quietly stops detecting
    shims fails this test instead of passing the one above.
    """
    shim = "src/provide/uterm/server/bridge/hub/router.py"
    impl = "src/provide/uterm/server/bridge/hub/router_impl.py"
    assert impl in _perimeter_paths(), "the impl is on the perimeter, so removing it is a real regression"

    gaps = _perimeter_gaps([entry for entry in _perimeter_paths() if entry != impl])

    assert any("router_impl" in gap and shim in gap for gap in gaps), gaps


def test_a_module_that_is_not_a_shim_is_left_alone() -> None:
    """Only pure re-exports are checked — a module with code of its own is not one."""
    assert _is_reexport_shim(ast.parse("from x import y\n\n__all__ = ['y']\n")) is True
    assert _is_reexport_shim(ast.parse("from x import y\n\ndef f():\n    return y\n")) is False
