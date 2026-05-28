<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Mutation Testing Patterns

This repo enforces **mutation testing** with [`mutmut`](https://mutmut.readthedocs.io/)
on a curated perimeter of security- and correctness-critical files. The gate
verifies that the test suite *kills* introduced mutations — i.e. that a test
actually fails when a mutated copy of the source changes behavior. A passing
line/branch-coverage number only proves a line was *executed*; mutation testing
proves it was *asserted on*.

- **Runner:** `scripts/run_mutation_gate.py`
- **Config:** `[tool.mutmut]` in the root `pyproject.toml`
- **Required score:** **100%** kill rate on the perimeter (`--min-mutation-score`
  defaults to `100.0`), with **zero** mutants in any bad state.
- **CI invocation:** `scripts/run_mutation_gate.py --changed-only` (via
  `ci/prepare_mutation_args.sh`), so the gate only fires on perimeter files a PR
  actually touched.

---

## How the perimeter is chosen

The perimeter is the explicit `paths_to_mutate` list in `[tool.mutmut]`. It is
**not** the whole tree — running mutmut everywhere would take hours and most
mutations on glue code are low-value. We enumerate only the files where a
silently-wrong mutation would be dangerous or expensive:

- **Security surfaces:** `auth.py`, tunnel `token_hash.py` (constant-time
  compare), tunnel `intercept.py` (header denylist + gating), server
  `routes/`, `webhooks.py`, `registry.py`, `config_schema.py`,
  `app/factory.py`, manager `process.py` / `config.py`.
- **State machines & boundary arithmetic:** `bridge/coordinator.py`
  (HijackCoordinator lease state machine), server `bridge/models.py`
  (`HijackLease.expire` boundary), the refactor-#16 hub services
  (`bridge/hub/{registry,limiter,lease,router,connection,presence,store,polling_service}.py`).
- **Wire format / single source of truth:** `bridge/schemas.py` (Pydantic
  frame models — the one place wire frames are defined), `bridge/contracts.py`
  (protocol-version negotiation), the control-channel codec/builders/patterns,
  detection `detector.py` / `engine.py`, `io.py`, `recording.py`,
  PTY `connector.py`, deckmux `_service.py`.

Two pieces of plumbing make the cross-package list work:

1. **Path prefixes.** Core (`provide-uterm`) contributes to the root `src/`
   namespace-merge tree, so its entries use the short `src/provide/uterm/...`
   form. Files in *other* packages do not appear there, so their entries use
   the full `packages/<pkg>/src/provide/uterm/...` prefix.
2. **`tests_dir` binding.** Each perimeter source needs its covering test
   suites listed in `tests_dir`; otherwise mutmut finds the mutants but binds
   them to zero tests and reports every one as `no_tests`. `also_copy` lists
   the source + test trees that must be copied into `mutants/` alongside the
   mutated source so imports resolve and discovery finds the suites.

When you add a file to `paths_to_mutate`, you almost always also add its test
suite(s) to `tests_dir` and the containing tree to `also_copy`.

---

## Running the gate

```bash
# Full perimeter (slow — runs the entire paths_to_mutate list):
uv run python scripts/run_mutation_gate.py

# Only files changed vs HEAD that fall under a mutation root (what CI runs):
uv run python scripts/run_mutation_gate.py --changed-only

# Only staged changes (useful in a pre-push check):
uv run python scripts/run_mutation_gate.py --changed-only --staged-only

# Compare against a different base ref (e.g. the PR base):
uv run python scripts/run_mutation_gate.py --changed-only --base-ref origin/main
```

`--changed-only` computes the changed `.py` files under `DEFAULT_MUTATION_ROOTS`
(in `run_mutation_gate.py`), maps each one back to the path mutmut actually uses
(an inode-based lookup that follows the root `src/` symlink tree), temporarily
rewrites `paths_to_mutate` in the root `pyproject.toml` to just those files,
runs mutmut, then restores the original config. If no changed file falls under a
mutation root, the gate **skips with exit 0** — a clean no-op.

The runner retries once in single-worker mode if the first parallel pass isn't
clean, to absorb flaky parallelism, then fails loudly with the stats if still
not 100%.

---

## Pass / fail accounting

`mutmut` classifies each mutant. The gate is clean only when the score is
`>= min_mutation_score` (100% by default) **and** `bad_total == 0`.

- **`killed`** — a test failed on the mutant. This is what we want.
- **`BAD_MUTANT_STATES`** (each must be zero): `not checked`, `survived`,
  `suspicious`, `timeout`, `skipped`. Any of these on a perimeter file fails
  the gate.
- **`BAD_STAT_KEYS`** (from the legacy stats path): `segfault`, `suspicious`,
  `no_tests`, `check_was_interrupted_by_user`. `no_tests` almost always means a
  `paths_to_mutate` entry has no matching `tests_dir` entry.

A **survivor** means a mutation changed behavior and *no test noticed* — your
tests under-specify that code. A `no_tests` mutant means the perimeter file
isn't wired to any test suite. Fix survivors by adding an assertion that pins
the exact behavior the mutation breaks; fix `no_tests` by adding the suite to
`tests_dir` (+ tree to `also_copy`).

---

## Common kill patterns

The `*_mutmut.py` test modules (e.g. `tests/test_auth_mutmut.py`,
`tests/test_io_mutmut.py`, `tests/detection/test_detector_detect_mutmut.py`,
`tests/terminal/test_control_channel_mutmut.py`) are surgical suites written
specifically to kill mutmut survivors. They share recurring techniques:

- **Assert on exact diagnostic/return shapes, not just truthiness.** mutmut
  loves to mutate dict keys, `reason=` strings, and accumulator contents.
  Assert `diag.regex_matched_but_failed[0]["reason"] == "negative_match"`, not
  just `assert diag.match is None`.
- **Pin both sides of every boundary.** For `>=` / `>` / off-by-one arithmetic
  (lease expiry, rate-limiter windows), test the value *at* the boundary and
  *one step past* it, so swapping the operator or `+1`/`-1` is observable.
- **Pin both branches of every conditional.** A negative-match short-circuit,
  an `expect_cursor_at_end` check, an early `return` — each needs a test that
  exercises the true path and one that exercises the false path.
- **Assert constant-time/security invariants behaviorally.** For
  `token_hash.py`, assert that compare returns `False` for a wrong token and
  `True` for the right one, and that the hashing is deterministic — so mutating
  the comparison or the digest is caught.
- **Assert string/operator constants.** Mutating `"+"` → `"-"`, `and` → `or`,
  or a literal string is caught only if a test depends on the exact output.

When adding a perimeter file, run the gate once, read the survivor list, and
write one targeted assertion per survivor rather than broad "exercise" tests.

---

## Adding a file to the perimeter — checklist

1. Add the source path to `paths_to_mutate` (short `src/...` form for core,
   full `packages/.../src/...` form otherwise).
2. Add its covering test suite(s) to `tests_dir`.
3. Add the containing source + test tree(s) to `also_copy`.
4. Run `uv run python scripts/run_mutation_gate.py` (or `--changed-only` while
   iterating) and drive survivors to zero.
5. Keep the inline comments in `[tool.mutmut]` accurate — they explain *why*
   each entry is on the perimeter.
