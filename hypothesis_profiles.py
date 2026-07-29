#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared Hypothesis settings profiles for every pytest entry point in the repo.

Loaded via :func:`activate` from the three conftests that sit at a pytest
rootdir: ``conftest.py`` (repo root), ``packages/provide-uterm/conftest.py``,
and ``packages/provide-uterm-server/tests/conftest.py``. The duplication is
forced by pytest: both packages carry their own ``[tool.pytest.ini_options]``,
so passing a path under either makes *it* the rootdir and the repo-root
conftest is never loaded.

Why this module exists
----------------------
Without it every run started from an empty example database and printed no
pasteable reproduction, so a Hypothesis failure in CI was *not* reproducible by
re-running it: the falsifying example was never persisted and no blob was
printed. Two things fix that, and they are deliberately paired:

``database``
    Pinned to ``<repo root>/.hypothesis/examples`` — an *absolute* path, not the
    cwd-relative default. pytest is invoked from the repo root *and* from
    ``packages/provide-uterm`` (``uv run --directory``), which would otherwise
    produce two unrelated corpora. One absolute path means one corpus, which in
    turn means CI can cache exactly one directory. A counterexample found by any
    run is replayed first by every later run, so a fixed bug that regresses
    fails immediately rather than after another lucky search.

``print_blob``
    Hypothesis defaults this to ``False``, so today a CI failure prints the
    falsifying example but nothing you can paste. With it on, every failure
    prints ``@reproduce_failure('<version>', b'...')``, which replays that exact
    example on any machine, independent of seed, database, or ``max_examples``.

Why not a forced global seed
----------------------------
``--hypothesis-seed`` / ``core.global_force_seed`` looks like the obvious
"record the seed" answer, but ``hypothesis/core.py::run_engine`` sets
``database_key = None`` whenever ``global_force_seed`` is set. Forcing a seed in
CI would therefore silently disable the example database we are caching — the
two mechanisms are mutually exclusive. Hypothesis also only prints the
``@seed(...)`` hint for *flaky* failures (``not state.failed_normally``), so for
an ordinary reproducible failure the blob is the artifact, not the seed.

The deterministic-run escape hatch is the ``repro`` profile below, which sets
``derandomize=True`` and ``database=None``: same examples every time, on any
machine, with no corpus interference. Use it to triage a suspected flake.

Profiles
--------
``dev``    (default off-CI) — 50 examples; fast inner loop.
``ci``     (default when ``$CI`` is set) — 250 examples.
``deep``   — 1000 examples; the nightly scheduled CI run.
``repro``  — derandomized, no database; byte-identical runs for triage.

Select with ``HYPOTHESIS_PROFILE=<name>``. Note that only the ~30 ``@given``
tests without an explicit ``@settings(max_examples=...)`` are affected by the
profile's ``max_examples``; an explicit decorator always wins.

Why the corpus is cached, not committed
---------------------------------------
Committing a seed corpus under version control was considered and rejected.
``DirectoryBasedExampleDatabase`` keys every entry by ``function_digest(test)``
— a hash over the test's name, module, and signature — so renaming or moving a
test silently orphans its entries with no error, only dead weight. The stored
choice sequences are format-versioned too, so a Hypothesis upgrade can orphan
the lot. And the entries are opaque binary blobs: a reviewer cannot tell from
the diff whether a new file is a real regression guard or noise, while every
concurrent branch that finds an example produces a conflicting binary add.

The durable, reviewable mechanism for "this example must always be checked"
already exists: promote the counterexample to an ``@example(...)`` decorator on
the test. That is typed, diffable, survives renames because it lives next to
the test, and runs on every invocation regardless of profile or corpus state.
Treat the cached corpus as a *warm start* — a speed-up and a regression tripwire
that is always safe to throw away — and ``@example`` as the permanent record.

Not active under mutmut
-----------------------
:func:`activate` is a no-op inside the ``mutants/`` tree. mutmut deliberately
runs property tests against mutated code (for example
``tests/tunnel/test_token_hash_hypothesis.py``), and those *correct* failures
must never be written into the corpus that normal runs replay. The mutation
gate's ``-p no:hypothesis`` only disables the pytest plugin, not ``@given``, so
this guard is load-bearing rather than incidental.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent
DATABASE_DIR: Final[Path] = REPO_ROOT / ".hypothesis" / "examples"

DEFAULT_PROFILE: Final[str] = "dev"
CI_PROFILE: Final[str] = "ci"

#: ``max_examples`` per profile. See the module docstring for the rationale.
PROFILE_MAX_EXAMPLES: Final[dict[str, int]] = {
    "dev": 50,
    "ci": 250,
    "deep": 1000,
    "repro": 100,
}


def selected_profile(env: dict[str, str] | None = None) -> str:
    """Return the profile name to load.

    ``HYPOTHESIS_PROFILE`` wins; otherwise ``ci`` when running under a CI
    provider (GitHub Actions sets ``CI=true``), else ``dev``.
    """
    environ = os.environ if env is None else env
    explicit = environ.get("HYPOTHESIS_PROFILE", "").strip()
    if explicit:
        return explicit
    return CI_PROFILE if environ.get("CI") else DEFAULT_PROFILE


def _under_mutmut() -> bool:
    """True when pytest is running from mutmut's ``mutants/`` copy of the tree."""
    return "MUTANT_UNDER_TEST" in os.environ or REPO_ROOT.name == "mutants"


def activate() -> str | None:
    """Register every profile and load the selected one.

    Returns the loaded profile name, or ``None`` when skipped (mutmut tree, or
    hypothesis not installed — the platform/client packages do not depend on it).
    """
    if _under_mutmut():
        return None
    try:
        from hypothesis import settings
        from hypothesis.database import DirectoryBasedExampleDatabase
    except ImportError:  # pragma: no cover - packages that don't install hypothesis
        return None

    # Created eagerly, not lazily on first save: CI's actions/cache/save step
    # errors out on a non-existent path, and a run that found no counterexample
    # would otherwise never create it.
    DATABASE_DIR.mkdir(parents=True, exist_ok=True)
    shared_database = DirectoryBasedExampleDatabase(DATABASE_DIR)
    for name, max_examples in PROFILE_MAX_EXAMPLES.items():
        is_repro = name == "repro"
        extra: dict[str, object] = {}
        if name == "deep":
            # deadline=None for the nightly run only. `deep` writes into the
            # default branch's corpus, so a DeadlineExceeded caused by nothing
            # but runner contention would be persisted as a "counterexample" and
            # replayed by every later build. Hangs are still bounded by
            # pytest-timeout. The `ci` profile keeps Hypothesis' default
            # deadline — unchanged from today's behaviour.
            extra["deadline"] = None
        settings.register_profile(
            name,
            max_examples=max_examples,
            # print_blob defaults to False; every profile turns it on so any
            # failure — local or CI — prints a pasteable @reproduce_failure.
            print_blob=True,
            derandomize=is_repro,
            database=None if is_repro else shared_database,
            **extra,  # type: ignore[arg-type]
        )

    name = selected_profile()
    settings.load_profile(name)
    return name
