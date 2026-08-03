# CM-07: Generated Capability Manifest

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace hand-maintained capability claims with a generated manifest
that records, per backend and per operation, whether it is served, unsupported,
or platform-specific — and which fails when a claim has no executable evidence.

**Architecture:** The canonical REST inventory already exists as
`API_ROUTE_REGISTRY` and `spec/uterm-api.yaml`. What is missing is a per-backend
classification derived from it, checked against what each backend actually
binds and what the live matrix actually proves. Generate the manifest from the
registry plus an explicit non-registry inventory plus a declared platform-only
list, then validate it against both the bound handlers and the live results.

**Tech Stack:** Python generator, JSON manifest, the existing live matrix and
conformance runners.

## Global Constraints

- Follows the repo codegen convention: generated file committed, AUTO-GENERATED
  banner, `--check` in pre-commit and CI, hand-editing forbidden.
- `spec/uterm-api.yaml`'s existing symbol checks remain. They are a lower-level
  static gate and this does not replace them — the quality-evidence design says
  so explicitly.
- Backends covered: Python, Go, C#, TypeScript.
- SPDX headers on new source files.

## Context

Measured 2026-08-03: no capability manifest exists in any form. `spec/`
contains `uterm-api.yaml` (346 lines), `behavior.json`, `behavior_vectors.json`,
and two scenario files.

Capability claims today live in prose across at least
`docs/ARCHITECTURE.md`, `docs/protocol-matrix.md`,
`docs/security-language-parity.md`, `docs/typescript-port-roadmap.md` and
several ARDs. Nothing checks any of them against the code.

The live matrix already reports unsupported cells. On run `30860934627`
(`814f6d87`, 2026-08-03) it reported:

```
160 cells: 154 ok, 2 failed, 0 errored, 4 unsupported; 0 not run
```

Those four unsupported cells were **not enumerated** by the measurement. This
plan is what enumerates them and forces each to be either justified or fixed.

Measured 2026-08-03; see
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`,
finding CM-07.

## File Structure

- `scripts/codegen_capability_manifest.py` — new generator and validator.
- `spec/capability_manifest.json` — new, generated, committed.
- `spec/non_registry_operations.json` — new, hand-maintained. The operational
  endpoints outside `API_ROUTE_REGISTRY` (health, readiness, metrics, security
  posture, asset serving). Hand-maintained because there is no registry to
  derive them from, and validated the same way so it cannot drift silently.
- `spec/platform_only_operations.json` — new, hand-maintained. Operations
  genuinely unavailable on a backend, each with a written reason.
- `tests/conformance/test_capability_manifest.py` — new.

---

### Task 1: Enumerate what each backend actually binds

**Files:**
- Create: `scripts/codegen_capability_manifest.py`
- Create: `spec/non_registry_operations.json`

**Interfaces:**
- Consumes: `packages/provide-uterm-ts/src/api-routes/routes.ts`
  (`API_ROUTES`, `API_ROUTE_REGISTRY`), `spec/uterm-api.yaml`.
- Produces: `scripts/codegen_capability_manifest.py` exposing
  `collect_bound_operations(backend: str) -> set[str]` and
  `load_operation_inventory() -> list[Operation]`. Task 2 consumes both.

- [ ] **Step 1: Establish the operation inventory**

Run:
```bash
cd /Volumes/data/pyv/provide-uterm
grep -c "capability" packages/provide-uterm-ts/src/api-routes/routes.ts
uv run python -c "import yaml,pathlib; d=yaml.safe_load(pathlib.Path('spec/uterm-api.yaml').read_text()); print(type(d), len(d))"
```

Record both counts. The registry is canonical for portable REST; the YAML is the
existing static gate. If they disagree on operation count, that disagreement is
itself a finding — record it before proceeding.

- [ ] **Step 2: Write the non-registry inventory**

Create `spec/non_registry_operations.json` listing the operational endpoints
that are not in `API_ROUTE_REGISTRY`. Find them per backend:

```bash
grep -rn '"/health"\|"/ready"\|"/metrics"\|/api/health' \
  packages/provide-uterm-server/src packages/provide-uterm-go/server \
  packages/provide-uterm-ts/src/server packages/provide-uterm-csharp/src \
  --include="*.py" --include="*.go" --include="*.ts" --include="*.cs" | head -40
```

Each entry gets an operation id, a method, a path, and a one-line description.

- [ ] **Step 3: Write the binding collector**

`collect_bound_operations(backend)` reports what a backend actually binds:

- **TypeScript** — read the handler table passed to `bindApiRoutes`. The
  registry already validates completeness at
  `packages/provide-uterm-ts/src/server/route-binding.ts:112`, so this reads
  the same source of truth.
- **Python** — enumerate the FastAPI app's routes from a constructed app.
- **Go** and **C#** — enumerate from their route tables.

Do not infer bindings by grepping for path strings. A path in a comment or a
test fixture would count, and a manifest built on grep hits is exactly the kind
of unverified claim this plan replaces.

- [ ] **Step 4: Verify the collector against a known backend**

Run:
```bash
uv run python -c "
from scripts.codegen_capability_manifest import collect_bound_operations
ops = collect_bound_operations('python')
print(len(ops)); print(sorted(ops)[:5])
"
```

Expected: a plausible count with recognisable operation ids. Cross-check three
by hand against the Python server's route table before trusting the number.

- [ ] **Step 5: Commit**

```bash
git add scripts/codegen_capability_manifest.py spec/non_registry_operations.json
git commit -m "feat(spec): enumerate what each backend actually binds

Capability claims live in prose across half a dozen docs and nothing
checks any of them against the code. This is the part that reads the
truth: bound handlers from each backend's own route table, plus an
explicit inventory for the operational endpoints that are not in the
shared registry.

Bindings are read from route tables rather than grepped for path
strings — a path in a comment would otherwise count as served."
```

---

### Task 2: Generate the manifest and fail on unjustified claims

**Files:**
- Modify: `scripts/codegen_capability_manifest.py`
- Create: `spec/capability_manifest.json`
- Create: `spec/platform_only_operations.json`
- Modify: `.pre-commit-config.yaml`, `ci/quality_checks.sh`

**Interfaces:**
- Consumes: Task 1's collector and inventory.
- Produces: `spec/capability_manifest.json`, one entry per (operation, backend)
  with a status of `served`, `unsupported`, or `platform_specific`.

- [ ] **Step 1: Write the failing validation test**

Create `tests/conformance/test_capability_manifest.py` asserting the five
conditions the quality-evidence design requires. Validation fails when:

1. a required operation lacks a backend classification;
2. a served operation lacks executable evidence;
3. a handler is bound but omitted from the manifest;
4. a portable operation is marked unsupported without an approved reason;
5. generated artifacts differ from committed files.

Run: `uv run pytest tests/conformance/test_capability_manifest.py -v`

Expected: FAIL — no manifest exists.

- [ ] **Step 2: Generate the manifest**

Run: `uv run python scripts/codegen_capability_manifest.py`

Expected: `spec/capability_manifest.json` is written. Every operation appears
once per backend. Status is `served` where the backend binds it,
`platform_specific` where `spec/platform_only_operations.json` says so with a
reason, and `unsupported` otherwise.

- [ ] **Step 3: Read the unsupported list — this is the deliverable**

Run:
```bash
uv run python -c "
import json
m = json.load(open('spec/capability_manifest.json'))
for e in m['entries']:
    if e['status'] == 'unsupported':
        print(e['backend'], e['operation'])
"
```

This is the first enumeration of what each backend does not serve. Expect the
TypeScript list to be substantial — its README describes a partial runtime port,
and CM-07 is what makes "partial" a specific list instead of a warning.

For each entry, decide and record: is it genuinely platform-specific (move it to
`platform_only_operations.json` with a written reason), or is it work (leave it
`unsupported` and file it)? An entry that is neither is what condition 4 fails on.

- [ ] **Step 4: Wire in the drift check**

Add `--check` to `.pre-commit-config.yaml` and `ci/quality_checks.sh` beside the
existing codegen checks.

Prove it works: edit one status in `spec/capability_manifest.json`, run
`--check`, confirm it fails, revert, confirm it passes.

- [ ] **Step 5: Run the validation**

Run: `uv run pytest tests/conformance/test_capability_manifest.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/codegen_capability_manifest.py spec/capability_manifest.json \
        spec/platform_only_operations.json tests/conformance/test_capability_manifest.py \
        .pre-commit-config.yaml ci/quality_checks.sh
git commit -m "feat(spec): generate the per-backend capability manifest

Every operation now has a status per backend, derived from what that
backend actually binds rather than from prose in a doc.

The unsupported list is the point: it is the first enumeration of what
each backend does not serve, and each entry must be either justified as
platform-specific with a written reason or filed as work. 'Partial
runtime port' becomes a specific list.

Validation fails on an unclassified operation, a bound-but-omitted
handler, a portable operation marked unsupported without a reason, and
generated-file drift."
```

---

### Task 3: Served means proven, and the live matrix agrees

**Files:**
- Modify: `scripts/codegen_capability_manifest.py`
- Modify: `tests/conformance/test_capability_manifest.py`

**Interfaces:**
- Consumes: the live matrix result output.
- Produces: manifest entries gain an `evidence` field naming the adapter or
  scenario that proves a `served` claim.

Condition 2 — "a served operation lacks executable evidence" — is the one that
makes the manifest worth more than the prose it replaces. A binding proves a
handler exists, not that it behaves.

- [ ] **Step 1: Enumerate the four unsupported live cells**

Run the live matrix and capture its per-cell output:

```bash
uv run pytest tests/conformance/live/ -v 2>&1 | tee /tmp/live-matrix.txt
grep -i "unsupported" /tmp/live-matrix.txt
```

Expected: four cells. The measurement on 2026-08-03 recorded the count and not
the identities; this is where they get names.

- [ ] **Step 2: Cross-check the manifest against the live results**

Add validation that every `served` entry has a live cell or a conformance
scenario that exercised it, and that every live `unsupported` cell corresponds
to a manifest entry marked `unsupported` or `platform_specific`.

A cell the live matrix skips while the manifest claims `served` is the exact
false claim this plan exists to catch.

- [ ] **Step 3: Reconcile every disagreement**

Run: `uv run pytest tests/conformance/test_capability_manifest.py -v`

Every failure is one of:
- manifest claims served, nothing proves it → add a scenario, or reclassify;
- live reports unsupported, manifest claims served → the manifest was wrong;
- live skips a cell silently → the skip needs a reason or the cell needs to run.

Fix each. Do not relax the validation to make it pass.

- [ ] **Step 4: Full run**

Run:
```bash
uv run pytest tests/conformance/ -v
uv run python scripts/codegen_capability_manifest.py --check
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add spec/capability_manifest.json scripts/codegen_capability_manifest.py \
        tests/conformance/test_capability_manifest.py
git commit -m "feat(spec): a served capability must name the evidence that proves it

A bound handler proves a handler exists, not that it behaves. Every
served entry now names the live cell or conformance scenario that
exercised it, and validation fails when it names none.

This also gives the four unsupported live cells their names. The
2026-08-03 measurement recorded the count and not the identities, which
is precisely the gap: a number nobody can act on."
```

---

### Task 4: Documentation reflects the manifest

**Files:**
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/protocol-matrix.md`
- Modify: `packages/provide-uterm-ts/README.md`

**Interfaces:**
- Consumes: `spec/capability_manifest.json`.
- Produces: nothing generated. Prose updates.

- [ ] **Step 1: Find every stale capability claim**

Run:
```bash
grep -rn "partial runtime port\|not yet supported\|unsupported" \
  docs/ packages/*/README.md | head -30
```

- [ ] **Step 2: Replace claims with pointers**

Each prose claim about what a backend supports either becomes a pointer to
`spec/capability_manifest.json` or is deleted. Per the quality-evidence design,
numbers that change mechanically are "either generated, described as
thresholds, or tied to dated evidence."

The TypeScript README's partial-runtime warning stays until its unsupported list
is empty — that is the TypeScript-parity design's own rollout condition, and
CM-07 makes the list checkable rather than removing the warning.

- [ ] **Step 3: Commit**

```bash
git add docs/ packages/provide-uterm-ts/README.md
git commit -m "docs: point capability claims at the manifest

Prose claims about what each backend supports were spread across half a
dozen documents with nothing checking them. They now point at the
generated manifest, which is checked.

The TypeScript partial-runtime warning stays. Its unsupported list is
not empty, and the parity design says the warning goes when the list
does — this makes the list checkable, not shorter."
```

---

## Definition of done

Per the measurement spec, CM-07 closes when:

- `spec/capability_manifest.json` exists, is generated, and its drift check was
  observed failing against a hand edit;
- all five validation conditions are enforced and pass;
- the four live-matrix unsupported cells are named, and each is either justified
  in `spec/platform_only_operations.json` or filed as work;
- no `served` entry lacks named evidence.

Then update the CM-07 row and the Status date in
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`.

## Notes for the implementer

- This plan will surface more work than it closes. That is its job: it converts
  "TypeScript is a partial port" from a sentence into a list. Do not shrink the
  list by marking things `platform_specific` without a real reason — condition 4
  exists to catch exactly that, and defeating it defeats the plan.
- The manifest is not a roadmap. An `unsupported` entry with a filed reason is a
  valid, honest end state; the design says a portable operation may be
  unsupported "only for a genuinely platform-specific operation named in the
  generated manifest," so anything else needs work filed against it.
- Run this plan *after* CM-01 through CM-06. Those change behavior, and a
  manifest generated mid-wave records a state that no longer holds by the time
  anyone reads it.
