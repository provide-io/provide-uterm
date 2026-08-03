# CM-09: Introduce a Shellcheck Gate

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a shellcheck gate covering all 29 shell scripts, fix what it
finds, and wire it into the quality gate so new scripts cannot regress.

**Architecture:** No shellcheck exists anywhere in the repo. Add it to
pre-commit (which already runs per-file hooks) and to `ci/quality_checks.sh`
(the single source CI and `make quality-gate` share). Fix findings by severity,
highest first, committing per script family so each change is reviewable.

**Tech Stack:** shellcheck, pre-commit, bash.

## Global Constraints

- `ci/quality_checks.sh` is the one source of truth for the static gate — CI's
  `quality` job and `make quality-gate` both run it. The check goes there, not
  into workflow YAML.
- Repo rule: no inline scripts in workflow YAML. If a `run:` block would exceed
  3 lines, it goes in `ci/`.
- Behavior of every script is preserved. A shellcheck fix that changes what a
  script does is a bug, not a fix.
- SPDX headers on any new file.

## Context

Measured 2026-08-03:

```
$ grep -rn "shellcheck" .github/workflows/ ci/
(no output)

$ find . -name "*.sh" -not -path "*/node_modules/*" -not -path "*/.venv/*" -not -path "*/.worktrees/*" | wc -l
29
```

The quality-evidence design says "Shellcheck warnings in language smoke, Colima
install, and VNC entrypoint scripts are fixed while preserving behavior." It
names specific findings, which implies shellcheck was run at review time — but
no gate produces them, so nothing keeps them fixed.

Scripts include `.ci/check_goldens.sh`, `.ci/vendor_cf_worker.sh`,
`ci/quality_checks.sh`, `ci/docker_language_smoke.sh`, `ci/live_matrix.sh`,
`ci/typecheck.sh`, `ci/hostile_probe.sh`, `scripts/generate_api_docs.sh` and 21
others.

Measured 2026-08-03; see
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`,
finding CM-09.

---

### Task 1: Measure the current state

**Files:**
- Create: `ci/shellcheck.sh`

**Interfaces:**
- Produces: `ci/shellcheck.sh`, invoked by `ci/quality_checks.sh` in Task 3 and
  by pre-commit in Task 4.

- [ ] **Step 1: Install shellcheck and get a baseline count**

Run:
```bash
command -v shellcheck || brew install shellcheck
cd /Volumes/data/pyv/provide-uterm
find . -name "*.sh" -not -path "*/node_modules/*" -not -path "*/.venv/*" \
       -not -path "*/.worktrees/*" -print0 | xargs -0 shellcheck -f gcc 2>&1 | wc -l
```

Record the number. It is the baseline this plan drives to zero.

- [ ] **Step 2: Group findings by severity and code**

Run:
```bash
find . -name "*.sh" -not -path "*/node_modules/*" -not -path "*/.venv/*" \
       -not -path "*/.worktrees/*" -print0 | xargs -0 shellcheck -f gcc 2>&1 \
  | grep -o "\[SC[0-9]*\]" | sort | uniq -c | sort -rn
```

This tells you whether it is a few shapes repeated or genuinely 29 different
problems. Fix the common shapes first.

- [ ] **Step 3: Write the runner script**

Create `ci/shellcheck.sh`:

```bash
#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Shellcheck every tracked shell script.
#
# Uses `git ls-files` rather than `find` so vendored trees, build output and
# worktrees are excluded by the same rules git already applies — a find-based
# sweep picks up node_modules and .venv copies and reports findings nobody owns.
set -euo pipefail

cd "$(dirname "$0")/.."

mapfile -t scripts < <(git ls-files '*.sh')

if [ "${#scripts[@]}" -eq 0 ]; then
  echo "no shell scripts found — the glob is wrong, not the repository" >&2
  exit 1
fi

echo "shellcheck: ${#scripts[@]} scripts"
shellcheck --severity=style --external-sources "${scripts[@]}"
```

The empty-list guard matters: a glob that silently matches nothing is a gate
that passes without checking anything, which is the failure mode this whole
convergence effort keeps finding.

- [ ] **Step 4: Run it**

Run: `bash ci/shellcheck.sh`

Expected: FAIL, listing findings. Record the count — it should match Step 1
allowing for the `git ls-files` scoping difference.

- [ ] **Step 5: Commit the runner only**

```bash
git add ci/shellcheck.sh
git commit -m "ci: add a shellcheck runner

No shellcheck existed anywhere in the repo, so the design's instruction
to 'fix shellcheck warnings in the language smoke, Colima install and
VNC entrypoint scripts' had no gate producing them.

Enumerates via git ls-files rather than find, so vendored trees and
worktrees are excluded by rules git already applies. Fails on an empty
list — a glob matching nothing is a gate that checks nothing."
```

---

### Task 2: Fix the findings

**Files:**
- Modify: shell scripts, grouped as Step 1 determines.

- [ ] **Step 1: Fix the highest-count code first**

Run: `bash ci/shellcheck.sh 2>&1 | grep -o "SC[0-9]*" | sort | uniq -c | sort -rn | head`

Common ones and their real fixes:

- **SC2086** (unquoted variable) — quote it. Where word-splitting is *intended*,
  use an array, not a disable comment.
- **SC2046** (unquoted command substitution) — same.
- **SC2155** (declare and assign separately) — split the line, so the assignment's
  exit status is not masked by `local`.
- **SC2164** (`cd` without `|| exit`) — `cd ... || exit 1`. Several scripts here
  `cd` and then operate on paths, which is precisely where a failed `cd` does
  damage.

- [ ] **Step 2: Fix, verifying behavior per script**

For each script, after fixing:

- if it has a test, run it;
- if it is a CI script, run it locally with its real arguments;
- if it cannot be run locally, read the diff and confirm the change is
  quoting-only.

`ci/quality_checks.sh` and `ci/typecheck.sh` are runnable locally — run them.

- [ ] **Step 3: Where a finding must be suppressed, suppress narrowly**

A `# shellcheck disable=SCxxxx` goes on the line it applies to, with a comment
saying why. Never at file scope. Never a blanket disable in a config file.

- [ ] **Step 4: Confirm zero findings**

Run: `bash ci/shellcheck.sh`

Expected: PASS, no output beyond the script count.

- [ ] **Step 5: Commit per script family**

One commit per group (`.ci/`, `ci/`, `scripts/`, `docker/`), not one commit for
all 29 files.

```bash
git add ci/
git commit -m "fix(ci): quote expansions and check cd in the ci/ scripts

Behavior-preserving: every change is quoting, an array where word
splitting was intended, or 'cd ... || exit'. Several scripts cd and then
operate on relative paths, which is where a failed cd does damage
silently."
```

---

### Task 3: Wire into the quality gate

**Files:**
- Modify: `ci/quality_checks.sh`

- [ ] **Step 1: Read how existing checks are invoked**

Run: `grep -n "^\(echo\|bash\|uv run\)" ci/quality_checks.sh | head -30`

Follow the same pattern — the same grouping output, the same failure behavior.

- [ ] **Step 2: Add the check**

Add the shellcheck invocation alongside the other static checks, in the same
style the file already uses.

- [ ] **Step 3: Verify the gate fails when a script regresses**

Introduce a finding: add an unquoted `$FOO` expansion to any tracked script.

Run: `make quality-gate`

Expected: FAIL, naming the script and the SC code.

Revert, run again, confirm PASS.

- [ ] **Step 4: Commit**

```bash
git add ci/quality_checks.sh
git commit -m "ci: run shellcheck in the quality gate

Goes in ci/quality_checks.sh, which CI's quality job and
make quality-gate both run, so a local gate and a CI gate cannot
disagree about it.

Verified it goes red against a reintroduced unquoted expansion."
```

---

### Task 4: Pre-commit hook

**Files:**
- Modify: `.pre-commit-config.yaml`

- [ ] **Step 1: Add the hook**

Add `shellcheck-py` to `.pre-commit-config.yaml` in the default (non-manual)
stage, matching how ruff and codespell are configured there. Use the same
severity as `ci/shellcheck.sh` so the two cannot disagree.

- [ ] **Step 2: Run against every file**

Run: `pre-commit run shellcheck --all-files`

Expected: PASS.

- [ ] **Step 3: Verify it fires on a new script**

Create a throwaway script with an unquoted expansion, `git add` it, and attempt
a commit.

Expected: the hook blocks the commit.

Delete the throwaway script.

- [ ] **Step 4: Commit**

```bash
git add .pre-commit-config.yaml
git commit -m "ci: shellcheck on every commit

Same severity as ci/shellcheck.sh so the hook and the gate cannot
disagree about what passes. Verified it blocks a new script with an
unquoted expansion."
```

---

## Definition of done

Per the measurement spec, CM-09 closes when:

- `bash ci/shellcheck.sh` reports zero findings across all tracked scripts;
- the gate was observed failing against a reintroduced finding;
- the pre-commit hook was observed blocking a new script with a finding;
- every suppression is line-scoped with a written reason — none at file scope;
- `make quality-gate` passes.

Then update the CM-09 row and the Status date in
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`.

## Notes for the implementer

- `ci/quality_checks.sh` is itself a shell script and will be checked by the
  gate it runs. Fix it like any other.
- Do not add a shellcheck config file that globally disables codes. The design
  says "Global warning suppression is not used" about C# warnings, and the same
  reasoning applies here — a global disable is a baseline wearing a different
  hat.
- If a script is genuinely dead, delete it rather than fixing it. Check
  `git log --oneline -- <script>` and grep for callers first.
