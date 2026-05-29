# Lane A6 — Infra / Docs / CI Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Read `00-ORCHESTRATION.md` "Global constraints" first.

**Goal:** Make the build/dependency story and the docs match reality, fix the inline-script policy violations, and restore type-check signal.

**Scope (exclusive write ownership):** root files only — `/CLAUDE.md`, `/pyproject.toml`, `/uv.lock`, `/.github/workflows/**`, `/ci/**`, `/.ci/**`, `/.pre-commit-config.yaml`, and creating `/MUTATION_PATTERNS.md`. Do NOT touch any `packages/**` file (the `packages/provide-uterm/pyproject.toml` `partial_branches` copy belongs to **A5**).

**Order:** INF-tel → INF-ci → INF-doc → INF-ty.

---

### Task 1 (INF-tel 🟠 High): Resolve the `provide-telemetry` editable-sibling discrepancy

**Files:** Modify `/pyproject.toml` (`[tool.uv.sources]`), regenerate `/uv.lock`; and/or correct `/CLAUDE.md`.

**Problem:** CLAUDE.md says `provide-telemetry` is an "editable install (sibling repo at `../provide-telemetry`)", but `uv.lock:1911` resolves it from **PyPI** (v0.4.7) with no `[tool.uv.sources]` entry. Local sibling edits are silently ignored. Also version-floor drift: root requires `>=0.4.4`, `packages/provide-uterm/pyproject.toml` requires `>=0.3`.

- [ ] **Step 1: Decide intent.** Confirm with the maintainer whether the editable sibling is the intended dev workflow. (Default assumption: yes, per CLAUDE.md.)
- [ ] **Step 2 (if editable intended):** Add to root `/pyproject.toml`:
  ```toml
  [tool.uv.sources]
  provide-telemetry = { path = "../provide-telemetry", editable = true }
  ```
  Then `uv sync --group dev` to regenerate `/uv.lock`. Verify `uv pip show provide-telemetry` points at the sibling path.
- [ ] **Step 2 (if PyPI intended instead):** Correct CLAUDE.md to state it is pinned from PyPI, and remove the "editable sibling" wording.
- [ ] **Step 3:** The per-package floor (`packages/provide-uterm/pyproject.toml:>=0.3`) is owned by **A5** — file a cross-lane request asking A5 to bump it to `>=0.4.4`. Do not edit that file from this lane.
- [ ] **Step 4: Commit** — `build: configure provide-telemetry editable sibling source` (or `docs: clarify provide-telemetry is pinned from PyPI`).

---

### Task 2 (INF-ci 🟠 High): Bring 3 workflows into inline-script policy compliance

**Files:** Modify `/.github/workflows/release.yml`, `/.github/workflows/hostile-client.yml`, `/.github/workflows/container-scan.yml`; create new scripts under `/ci/`.

**Policy (user standing rule):** no `run:` block >3 lines in workflow YAML — extract to a `ci/` script invoked in one line. `ci.yml` already complies; these three do not. Before creating a script, check whether an existing `ci/` script or Makefile target already does the work (reuse with overrides).

Violations to fix:
- `release.yml:103` (~10 lines), `release.yml:220` (~10), `release.yml:116` (~4), `release.yml:233` (~4)
- `hostile-client.yml:60` (~9), `:72` (~8), `:83` (~9), `:95` (~9)
- `container-scan.yml:51` (~4)

- [ ] **Step 1: Read** each flagged step and confirm exact line counts (they may have shifted).
- [ ] **Step 2:** For each block >3 lines, extract its body to a new `/ci/<name>.sh` (with the SPDX header from the global constraints, `#!/usr/bin/env bash` + `set -euo pipefail`), `chmod +x`, and replace the `run:` block with a single-line invocation. Add a one-line comment above every step describing what it does (per CLAUDE.md). Reuse existing scripts where one already covers the logic.
- [ ] **Step 3:** Validate YAML parses (`uv run python -c "import yaml,glob; [yaml.safe_load(open(f)) for f in glob.glob('.github/workflows/*.yml')]"`). If `actionlint` is available, run it.
- [ ] **Step 4: Commit** — one commit per workflow: `ci: extract inline scripts from release workflow`, etc.

---

### Task 3 (INF-doc 🟡): Fix dead/inaccurate doc references

**Files:** Create `/MUTATION_PATTERNS.md`; modify `/CLAUDE.md`.

**Problem:**
- `MUTATION_PATTERNS.md` is referenced by CLAUDE.md AND `/pyproject.toml` `[tool.mutmut]` comments but does not exist.
- CLAUDE.md says pre-commit runs mypy/ty/vitest "on commit" — they are `stages: [manual]` in `.pre-commit-config.yaml` (a normal commit runs only ruff/reuse/codespell/bandit/detect-secrets/codegen).
- (Reviewer note) CLAUDE.md's claim that the core `provide/uterm/__init__.py` uses `__getattr__` lazy loading is inaccurate — it uses eager imports; the lazy pattern is in the *client* package's `__init__`.

- [ ] **Step 1:** Create `/MUTATION_PATTERNS.md` documenting the mutation-testing patterns actually in use (derive content from `[tool.mutmut].paths_to_mutate`, `scripts/run_mutation_gate.py`, and existing `mutant`-marked tests). Include: how the perimeter is chosen, common kill patterns, how to run `--changed-only`, and what `BAD_MUTANT_STATES` covers. Add the SPDX header.
- [ ] **Step 2:** Correct CLAUDE.md "Pre-commit Hooks" section to list what actually runs on commit vs `[manual]` stage. Either update the doc to match `.pre-commit-config.yaml`, OR (if the maintainer wants them enforced) move mypy/ty/vitest off `stages: [manual]` — confirm intent first; default to fixing the doc.
- [ ] **Step 3:** Fix the `__getattr__` wording in CLAUDE.md's architecture section (core uses eager imports; client uses lazy).
- [ ] **Step 4: Commit** — `docs: add MUTATION_PATTERNS.md and correct CLAUDE.md drift`.

---

### Task 4 (INF-ty 🟡): Scope down the global `ty` ignore

**Files:** Modify `/pyproject.toml` `[tool.ty.rules]`.

**Problem:** `[tool.ty.rules]` globally ignores `invalid-argument-type` (a substantive correctness check) and `unused-type-ignore-comment` tree-wide. Combined with `ty` being informational-only in CI, `ty` provides little signal.

- [ ] **Step 1:** Run `uv run ty check packages/*/src/` with the global ignore removed to enumerate where `invalid-argument-type` actually fires.
- [ ] **Step 2:** If the firings trace to a single known-buggy module / upstream ty bug, scope the ignore to that path (per-file/per-module) instead of globally; otherwise fix the real type errors. Keep `unused-type-ignore-comment` global only if it is noise from the cross-package `type: ignore` debt — document why.
- [ ] **Step 3:** Confirm `uv run ty check` is clean (or fails only on intentionally-scoped suppressions).
- [ ] **Step 4: Commit** — `build: scope ty invalid-argument-type ignore instead of muting tree-wide`.

---

### Done criteria (Lane A6)
- [ ] `uv run python scripts/codegen_frames.py --check` passes (no accidental schema/TS touch)
- [ ] All workflow YAML parses; no `run:` block >3 lines remains in the three workflows
- [ ] `uv run pip-audit` runs clean (or known-accepted advisories documented)
- [ ] `/MUTATION_PATTERNS.md` exists with the SPDX header; CLAUDE.md matches reality
- [ ] Commits, one logical unit each.

### Cross-lane requests
- **A5:** bump `provide-telemetry` floor in `packages/provide-uterm/pyproject.toml` from `>=0.3` to `>=0.4.4`.
- **A5:** the `partial_branches` escape-hatch in `packages/provide-uterm/pyproject.toml` is handled in A5 Task 6 (do not touch from this lane).
