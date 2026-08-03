# CM-08: Make `ty` Diagnostic-Clean and Enforce It

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 73 current `ty` diagnostics and make `ci/typecheck.sh ty` fail
on any diagnostic instead of printing a warning and exiting 0.

**Architecture:** Fix by package, smallest first, so each commit is reviewable
and the count only goes down. Flip the gate last — flipping it first would just
turn CI red for the duration of the work.

**Tech Stack:** Python 3.11+, `ty` (Astral), `uv`.

## Global Constraints

- No baseline file, no ignore count, no per-diagnostic suppression added to make
  the gate pass. The quality-evidence design is explicit: "A baseline or ignored
  diagnostic count is not the target."
- `mypy --strict` must keep passing throughout. `SOFT_PACKAGES` is already empty,
  so mypy is fully strict — a fix that satisfies `ty` and breaks mypy is not a fix.
- No behavior changes. Every diagnostic here is resolved by a type declaration,
  a removed redundant construct, or a real type error being corrected.
- 100% branch coverage still enforced.

## Context

`ci/typecheck.sh:51` — "Informational only — never fail the gate."
`ci/typecheck.sh:60` — `uv run ty check "${pkg}" || rc=$?`
`ci/typecheck.sh:64` — `echo "::warning::ty reported issues (informational only)"`
`ci/typecheck.sh:66` — `exit 0`

The script's own header says "the repository is expected to stay
diagnostic-clean," so this plan implements an intent already written down.

Measured 2026-08-03, per strict package:

| Package | Diagnostics |
|---|---|
| `provide-uterm-annotation` | 0 |
| `provide-uterm-client` | 0 |
| `provide-uterm-platform` | 0 |
| `provide-uterm` (core) | 2 |
| `provide-uterm-cloudflare` | 23 |
| `provide-uterm-server` | 48 |
| **total** | **73** |

By category:

| Category | server | cloudflare | core |
|---|---|---|---|
| `invalid-argument-type` | 37 | 5 | — |
| `unused-ignore-comment` | 6 | 11 | — |
| `unsupported-operator` | 2 | — | — |
| `unresolved-import` | — | 2 | — |
| `invalid-method-override` | — | 2 | — |
| `invalid-assignment` | — | 2 | — |
| `redundant-cast` | 1 | — | 1 |
| `unresolved-attribute` | 1 | — | — |
| `invalid-type-form` | 1 | — | — |
| `unsupported-base` | — | 1 | — |
| `unused-ignore-comment` (core) | — | — | 1 |

Three packages are already clean, which is what makes the gate flip realistic.

Measured 2026-08-03; see
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`,
finding CM-08.

---

### Task 1: Core package — 2 diagnostics

**Files:**
- Modify: `packages/provide-uterm/src/provide/uterm/shell/commands/render.py:39`
- Modify: one further file, identified in Step 1.

- [ ] **Step 1: See the diagnostics**

Run: `uv run ty check packages/provide-uterm/src/`

Expected: 2 diagnostics. One is known:

```
warning[redundant-cast]: Value is already of type `Literal["truecolor", "256", "16"]`
  --> packages/provide-uterm/src/provide/uterm/shell/commands/render.py:39:20
```

- [ ] **Step 2: Fix the redundant cast**

Read `render.py:30-45` first. The `cast` is redundant because the preceding
guard already narrows `_mode_raw`. Remove it:

```python
            mode = _mode_raw
```

Only do this if the guard genuinely narrows. If it does not and `ty` is wrong,
that is a `ty` bug worth reporting upstream and the cast stays — record which,
rather than silencing it.

- [ ] **Step 3: Fix the second diagnostic**

Read what Step 1 printed and fix it the same way: a real declaration, not a
suppression.

- [ ] **Step 4: Verify both checkers**

Run:
```bash
uv run ty check packages/provide-uterm/src/
uv run mypy packages/provide-uterm/src/
uv run pytest packages/provide-uterm/tests -q
```

Expected: `ty` reports 0, mypy passes, tests pass at 100% coverage.

- [ ] **Step 5: Commit**

```bash
git add packages/provide-uterm/src/
git commit -m "fix(types): clear the core package's ty diagnostics

Two diagnostics, both redundant constructs rather than real type errors.
Removed rather than suppressed — the point of the gate is that a
suppression count is not a clean bill."
```

---

### Task 2: Cloudflare package — 23 diagnostics

**Files:**
- Modify: files under `packages/provide-uterm-cloudflare/src/`, identified in
  Step 1.

- [ ] **Step 1: Group the diagnostics**

Run:
```bash
uv run ty check packages/provide-uterm-cloudflare/src/ 2>&1 | grep -o "^\(error\|warning\)\[[a-z-]*\]" | sort | uniq -c | sort -rn
```

Expected:
```
  11 warning[unused-ignore-comment]
   5 error[invalid-argument-type]
   2 error[unresolved-import]
   2 error[invalid-method-override]
   2 error[invalid-assignment]
   1 warning[unsupported-base]
```

- [ ] **Step 2: Remove the 11 unused ignore comments**

These are `# type: ignore` comments that no longer suppress anything — type
debt that was paid off without the marker being removed. Deleting them is safe
and mechanical, but verify each with mypy afterwards: mypy and `ty` disagree
about some constructs, and an ignore unused by one may be load-bearing for the
other.

Run: `uv run mypy packages/provide-uterm-cloudflare/src/`

Expected: passes. If removing an ignore breaks mypy, restore that one and note
it — that is a genuine checker disagreement, not debt.

- [ ] **Step 3: Fix the 2 unresolved imports**

Read them first. In this package they are most likely the Cloudflare Workers
runtime modules, which exist only inside the Worker sandbox and are vendored
under `python_modules`. If so, the fix is a typed stub or a declared
runtime-only import — **not** a blanket ignore. Check
`.ci/check_cf_vendor_tree.sh` for how the vendored tree is already handled.

- [ ] **Step 4: Fix the remaining 10**

`invalid-method-override` (2), `invalid-assignment` (2), `unsupported-base` (1)
and `invalid-argument-type` (5) are real type statements that do not hold. Fix
the declaration or the code. An override whose signature does not match its base
is a bug waiting for a caller that uses the base type.

- [ ] **Step 5: Verify**

Run:
```bash
uv run ty check packages/provide-uterm-cloudflare/src/
uv run mypy packages/provide-uterm-cloudflare/src/
uv run pytest packages/provide-uterm-cloudflare/tests -q
bash .ci/check_cf_vendor_tree.sh
```

Expected: `ty` reports 0, everything else passes.

- [ ] **Step 6: Commit**

```bash
git add packages/provide-uterm-cloudflare/src/
git commit -m "fix(types): clear the Cloudflare package's ty diagnostics

Eleven of the twenty-three were ignore comments that stopped suppressing
anything — type debt paid off without the marker being removed. The
remaining twelve were declarations that did not hold, including two
method overrides whose signatures did not match their base."
```

---

### Task 3: Server package — 48 diagnostics

**Files:**
- Modify: files under `packages/provide-uterm-server/src/`, identified in
  Step 1.

- [ ] **Step 1: Group the diagnostics**

Run:
```bash
uv run ty check packages/provide-uterm-server/src/ 2>&1 | grep -o "^\(error\|warning\)\[[a-z-]*\]" | sort | uniq -c | sort -rn
```

Expected:
```
  37 error[invalid-argument-type]
   6 warning[unused-ignore-comment]
   2 error[unsupported-operator]
   1 warning[redundant-cast]
   1 error[unresolved-attribute]
   1 error[invalid-type-form]
```

- [ ] **Step 2: Find the pattern behind the 37**

Run:
```bash
uv run ty check packages/provide-uterm-server/src/ 2>&1 | grep -A1 "invalid-argument-type" | grep "\-\->" | sed 's/.*--> //' | cut -d: -f1 | sort | uniq -c | sort -rn
```

37 of one category is a handful of shapes repeated, not 37 independent bugs.
Expect them to cluster — the quality-evidence design names the likely ones:
"iterator typing, optional runtime imports, mixin protocols, and dynamic server
delegates receive real typed interfaces."

Fix each cluster once and re-count. **Do not fix 37 call sites individually if
one Protocol declaration fixes 30 of them** — that is the difference between
clearing diagnostics and improving the types.

- [ ] **Step 3: Fix the clusters, re-counting after each**

Run after each cluster: `uv run ty check packages/provide-uterm-server/src/ 2>&1 | tail -1`

Commit per cluster rather than in one batch. A 48-diagnostic commit is not
reviewable.

Note the hub composes nine service classes
(`packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/core.py`)
and forwards legacy facade methods to them. Dynamic forwarding is exactly what
produces `invalid-argument-type` in bulk, and a typed Protocol per service is
the fix that generalises.

- [ ] **Step 4: Fix the remaining 11**

`unused-ignore-comment` (6), `unsupported-operator` (2), `redundant-cast` (1),
`unresolved-attribute` (1), `invalid-type-form` (1). The `unresolved-attribute`
is the one to read carefully — it can be a real bug rather than a typing gap.

- [ ] **Step 5: Verify**

Run:
```bash
uv run ty check packages/provide-uterm-server/src/
uv run mypy packages/provide-uterm-server/src/
uv run pytest packages/provide-uterm-server/tests -q
```

Expected: `ty` reports 0.

- [ ] **Step 6: Commit**

Per cluster, as Step 3 says. Final message shape:

```bash
git commit -m "fix(types): give the hub service delegates real typed interfaces

Thirty-seven of the server's forty-eight ty diagnostics were one shape
repeated: the hub facade forwards to nine composed services through
dynamic delegation, and nothing declared what those services accept.

A Protocol per service fixes the cluster at its source rather than
annotating each call site, which is the difference between clearing
diagnostics and improving the types."
```

---

### Task 4: Flip the gate

**Files:**
- Modify: `ci/typecheck.sh:50-67`

- [ ] **Step 1: Confirm every package is clean**

Run: `bash ci/typecheck.sh ty`

Expected: no diagnostics from any package. Confirm by count:

```bash
for p in packages/provide-uterm/src/ packages/provide-uterm-cloudflare/src/ \
         packages/provide-uterm-annotation/src/ packages/provide-uterm-server/src/ \
         packages/provide-uterm-client/src/ packages/provide-uterm-platform/src/; do
  echo "$p $(uv run ty check "$p" 2>&1 | grep -c '^\(error\|warning\)\[')"
done
```

Expected: `0` for all six.

- [ ] **Step 2: Make the gate fail on diagnostics**

Replace the `ty)` case body:

```bash
  ty)
    # Enforced. The header used to say the repository "is expected to stay
    # diagnostic-clean" while the gate exited 0 regardless, so nothing held it
    # to that — 73 diagnostics had accumulated by 2026-08-03.
    packages=("${STRICT_PACKAGES[@]}")
    if [ "${#SOFT_PACKAGES[@]}" -gt 0 ]; then
      packages+=("${SOFT_PACKAGES[@]}")
    fi
    for pkg in "${packages[@]}"; do
      echo "::group::ty ${pkg}"
      uv run ty check "${pkg}"
      echo "::endgroup::"
    done
    ;;
```

`set -euo pipefail` is already at the top, so the first failing `ty check` exits
non-zero. Remove the `rc` accumulation and the `::warning::` line.

Also update the header comment at `ci/typecheck.sh:16-17`, which currently
describes `ty` as informational.

- [ ] **Step 3: Prove the gate has teeth**

Temporarily reintroduce a diagnostic — add `x: int = "not an int"` to any module
under a strict package.

Run: `bash ci/typecheck.sh ty`

Expected: FAIL, non-zero exit, naming the file.

Revert: `git checkout <that file>`

Run again: passes.

- [ ] **Step 4: Run the full quality gate**

Run: `make quality-gate`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ci/typecheck.sh
git commit -m "ci: fail the type gate on any ty diagnostic

The script's own header said the repository is expected to stay
diagnostic-clean, and the gate printed a warning and exited 0 regardless.
Nothing held it to that, and 73 diagnostics had accumulated by the time
anyone counted.

Verified the gate goes red against a reintroduced diagnostic."
```

---

## Definition of done

Per the measurement spec, CM-08 closes when:

- all six strict packages report 0 `ty` diagnostics;
- `ci/typecheck.sh ty` exits non-zero on any diagnostic, verified against a
  deliberately reintroduced one;
- no baseline file, ignore count, or new blanket suppression was added;
- `mypy --strict`, the test suites, and `make quality-gate` all still pass.

Then update the CM-08 row and the Status date in
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`.

## Notes for the implementer

- Fix in the order given. Core (2) then Cloudflare (23) then server (48) means
  every commit lands against a smaller remaining count, and the gate flip
  happens once rather than being fought during the work.
- Where `ty` and mypy genuinely disagree, mypy wins — it is the enforced gate
  today and has been for longer. Record the disagreement in the commit message
  rather than contorting the code to satisfy both.
- `ty` is pre-1.0 and its diagnostics change between releases. If a version bump
  introduces new diagnostics, that is ordinary maintenance now that the gate is
  enforced, and it is the intended trade: a gate that occasionally needs
  attention beats one that silently accumulates 73 findings.
