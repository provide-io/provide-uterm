#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Every demo the site publishes must be recordable, and must describe itself.

``demo/site-manifest.json`` is what uterm.io renders. It is built by importing
each ``scripts.demos.record_<feature>`` module and reading metadata constants
off it, so a module that does not expose them yields a demo with no title, no
subtitle, no description and a 0.0 duration -- and nothing fails, because
``getattr(module, "TITLE", default)`` is a silent fallback.

That was live on 2026-09-03. ``record_deckmux.py`` had been split for the LOC
limit into a shim that re-exported ``record`` and nothing else, leaving all four
constants behind in ``record_deckmux_impl.py``. The committed manifest still
looked correct only because it had not been regenerated since the split; the
next regeneration would have published "Deckmux" with empty prose. This is the
same shape as the mutation perimeter's shim problem -- a split that carries the
callable and drops everything else -- so it is guarded the same way.

The second test keeps the two lists of demos honest about each other. There are
three: the manifest builder's, the reel's, and the orchestrator's. The
orchestrator's was missing ``demo_grid``, so "re-record the demos" would
quietly have skipped one that the site publishes.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEMOS_DIR = REPOSITORY_ROOT / "scripts" / "demos"

#: Read by ``build_site_manifest._feature_metadata``. A missing one is not an
#: error there -- it is a default -- which is exactly why it needs a test.
REQUIRED_CONSTANTS = ("FEATURE", "TITLE", "SUBTITLE", "DESCRIPTION")


def _module_names(path: Path) -> set[str]:
    """Names reachable as attributes of the module at *path*.

    Read rather than imported: a recorder pulls in the whole demo harness, and
    the question here is only what the module exposes. A shim exposes what it
    re-exports, so ``from .impl import TITLE`` counts exactly as an assignment
    does -- which is the distinction the test exists to make.
    """
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def _manifest_feature_keys() -> list[str]:
    """The demo ids ``build_site_manifest.py`` publishes to the site."""
    source = (DEMOS_DIR / "build_site_manifest.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "FEATURES":
            return [entry.value for entry in node.value.elts]
    raise AssertionError("scripts/demos/build_site_manifest.py no longer defines FEATURES")


def _orchestrator_feature_keys() -> list[str]:
    """The feature ids ``scripts/record_all_demos.py`` will actually record."""
    source = (REPOSITORY_ROOT / "scripts/record_all_demos.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "FEATURES":
            return [entry.elts[1].value for entry in node.value.elts]
    raise AssertionError("scripts/record_all_demos.py no longer defines FEATURES")


def test_every_published_demo_module_exposes_its_metadata() -> None:
    """A recorder that hides its constants publishes an empty demo card."""
    incomplete: list[str] = []
    for feature in _manifest_feature_keys():
        path = DEMOS_DIR / f"record_{feature}.py"
        assert path.is_file(), f"{path} is published in the manifest but does not exist"
        missing = [name for name in REQUIRED_CONSTANTS if name not in _module_names(path)]
        if missing:
            incomplete.append(f"scripts/demos/record_{feature}.py is missing {', '.join(missing)}")

    assert not incomplete, (
        "These modules would publish a demo with blank metadata. If the file was split for the "
        "LOC limit, re-export the constants from the shim, not just `record`:\n  " + "\n  ".join(incomplete)
    )


def test_the_orchestrator_records_exactly_what_the_manifest_publishes() -> None:
    """Any demo on the site must be one `record_all_demos.py` re-records."""
    orchestrated = set(_orchestrator_feature_keys())
    published = set(_manifest_feature_keys())

    assert published - orchestrated == set(), (
        f"published but never re-recorded: {sorted(published - orchestrated)} — "
        "a re-record would leave these at their previous take"
    )
    assert orchestrated - published == set(), (
        f"recorded but not published: {sorted(orchestrated - published)} — "
        "add them to build_site_manifest.FEATURES or drop them from the orchestrator"
    )
