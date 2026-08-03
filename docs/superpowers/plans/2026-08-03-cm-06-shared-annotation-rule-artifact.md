# CM-06: Shared Annotation Rule Artifact

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the annotation rule inventory in a form every port can read,
and prove by fixture that all four detectors produce identical annotations —
including under arbitrary chunking.

**Architecture:** Python's `BUILTIN_RULES` is the de-facto canonical inventory
but lives in Python source, so the other three ports each maintain a hand-copied
version. Generate a committed language-neutral artifact from `BUILTIN_RULES`,
add a drift check the way the repo already does for frame schemas, and add
streaming fixtures that split input at every position.

**Tech Stack:** Python (generator + oracle), Go, C#, TypeScript, JSON artifact,
pytest conformance runner.

## Global Constraints

- The generator follows the existing codegen pattern:
  `scripts/codegen_frames.py` generates `spec`-adjacent artifacts, is run with
  `--check` in pre-commit and CI, and the generated file carries an
  AUTO-GENERATED banner. Hand-editing a generated file is forbidden.
- SPDX headers on new source files.
- Python's `BUILTIN_RULES` remains the single source of truth. The artifact is
  generated from it, never the reverse.
- No port's public API changes.

## Context

`packages/provide-uterm-annotation/src/provide/uterm/annotation/_rules.py:20`
defines `BUILTIN_RULES: list[DetectionRule]`. It is re-exported from the
package's `__init__.py` and consumed by `_detector.py:31`.

Measured 2026-08-03:

```
$ find . -name "rules.json" -not -path "*/node_modules/*"
(no output)
```

No language-neutral artifact exists, so no port can consume one. Each maintains
its own inventory — C# in `src/Provide.Uterm/Annotation/Detector.cs`, with Go
and TypeScript equivalents. Whether they currently agree was **not measured**;
this plan establishes it by fixture rather than by inspection, which is the
whole point.

The semantic-safety design says: "If direct generation is practical, a committed
language-neutral rules artifact is generated from one source of truth;
otherwise the conformance runner enforces exact rule parity." Generation is
practical, so this plan does that, and adds the runner anyway — the artifact
proves the rules match, the runner proves the *detectors* do.

Measured 2026-08-03; see
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`,
finding CM-06.

## File Structure

- `scripts/codegen_annotation_rules.py` — new generator, modelled on
  `scripts/codegen_frames.py`.
- `spec/annotation_rules.json` — new, generated, committed, AUTO-GENERATED
  banner.
- `spec/annotation_streaming_scenarios.json` — new fixture family.
- `tests/conformance/test_annotation_parity.py` — new runner.
- Per-port loaders, one per language, in Task 3.

---

### Task 1: Generate the rule artifact from BUILTIN_RULES

**Files:**
- Create: `scripts/codegen_annotation_rules.py`
- Create: `spec/annotation_rules.json`
- Modify: `.pre-commit-config.yaml`
- Modify: `ci/quality_checks.sh`

**Interfaces:**
- Consumes: `provide.uterm.annotation.BUILTIN_RULES` and the `DetectionRule`
  model in `_models.py`.
- Produces: `spec/annotation_rules.json`, consumed by Tasks 2 and 3.

- [ ] **Step 1: Read the existing codegen script**

Run: `sed -n '1,60p' scripts/codegen_frames.py`

Follow its structure exactly: the same `--check` flag, the same banner text
shape, the same exit codes. A second codegen script that behaves differently
from the first is its own maintenance problem.

- [ ] **Step 2: Write the failing check**

Run: `uv run python scripts/codegen_annotation_rules.py --check`

Expected: FAIL — the script does not exist.

- [ ] **Step 3: Write the generator**

Create `scripts/codegen_annotation_rules.py`. It imports `BUILTIN_RULES`, dumps
each rule's fields in a stable order, and writes
`spec/annotation_rules.json` with a banner. Key requirements:

- Sort rules by a stable key (rule id) so the output does not depend on list
  order in Python source.
- Emit every field of `DetectionRule`. Read `_models.py` and enumerate them
  explicitly rather than using `model_dump()` wholesale, so a field added later
  is a deliberate decision about the wire artifact rather than a silent
  addition.
- Pattern strings are emitted verbatim. Do not normalise, recompile, or
  re-escape them — the ports must receive exactly what Python matches with.
- `--check` regenerates into memory and compares against the committed file,
  exiting non-zero on any difference.

- [ ] **Step 4: Generate and inspect**

Run:
```bash
uv run python scripts/codegen_annotation_rules.py
python -c "import json;d=json.load(open('spec/annotation_rules.json'));print(len(d['rules']))"
```

Expected: a rule count matching `len(BUILTIN_RULES)`. Verify:

```bash
uv run python -c "from provide.uterm.annotation import BUILTIN_RULES; print(len(BUILTIN_RULES))"
```

The two numbers must match.

- [ ] **Step 5: Wire the drift check into pre-commit and CI**

In `.pre-commit-config.yaml`, add a hook beside the existing `codegen-frames`
one, with the same stage configuration.

In `ci/quality_checks.sh`, add the `--check` invocation beside the existing
codegen-frames check.

- [ ] **Step 6: Prove the drift check works**

Temporarily edit `spec/annotation_rules.json` — change one rule's id.

Run: `uv run python scripts/codegen_annotation_rules.py --check`

Expected: FAIL, naming the drift.

Revert: `git checkout spec/annotation_rules.json`

Run the check again and confirm it passes. A generated-file check that cannot
fail is not a check.

- [ ] **Step 7: Commit**

```bash
git add scripts/codegen_annotation_rules.py spec/annotation_rules.json \
        .pre-commit-config.yaml ci/quality_checks.sh
git commit -m "feat(spec): generate the annotation rule inventory from Python

BUILTIN_RULES is the canonical inventory and lives in Python source, so
the other three ports each keep a hand-copied version and nothing checks
that the copies still agree.

Generate a committed language-neutral artifact from it, with the same
--check drift gate the frame schemas already use. Fields are enumerated
explicitly rather than dumped wholesale, so a field added later is a
decision about the artifact rather than a silent addition.

Verified the check goes red against a hand-edited rule id."
```

---

### Task 2: Streaming scenarios that split at every position

**Files:**
- Create: `spec/annotation_streaming_scenarios.json`
- Create: `tests/conformance/test_annotation_parity.py`

**Interfaces:**
- Consumes: `spec/annotation_rules.json` from Task 1.
- Produces: `spec/annotation_streaming_scenarios.json`, consumed by Task 3.

- [ ] **Step 1: Write the scenario file**

Read `spec/fanout_security_scenarios.json` first and follow its schema.

Each scenario names an input byte string and the annotations expected from it.
The runner then feeds that input several ways and asserts the same result every
time. Six scenarios:

| Scenario ID | Input shape | Why it is here |
|---|---|---|
| `annot_001_single_match` | one command that matches one rule | baseline |
| `annot_002_no_match` | text matching nothing | a detector that emits on no match fails here |
| `annot_003_adjacent_matches` | two matches with no separator | boundary between consecutive emissions |
| `annot_004_overlapping_candidates` | input where a shorter rule's match sits inside a longer rule's candidate | which rule wins, and that only one fires |
| `annot_005_repeated_command` | the same matching command three times | three emissions, not one deduplicated emission |
| `annot_006_longest_rule_suffix` | a match straddling a chunk boundary at the longest rule's length | the retained-suffix bound |

- [ ] **Step 2: Write the runner with four feed modes**

`tests/conformance/test_annotation_parity.py` runs every scenario four ways
against each backend:

1. **one-shot** — the whole input in a single feed;
2. **byte-at-a-time** — one byte per feed;
3. **split-at-every-position** — for input of length *n*, *n-1* separate runs,
   each splitting into exactly two chunks at a different offset;
4. **random chunking** — several runs with varied chunk sizes, derived from a
   fixed seed recorded in the scenario file so a failure reproduces.

All four must yield identical annotations and no duplicate emissions. Mode 3 is
the one that catches suffix-retention bugs, and it is why the inputs are kept
short — *n-1* runs per scenario per backend adds up.

- [ ] **Step 3: Run against Python**

Run: `uv run pytest tests/conformance/test_annotation_parity.py -v`

Expected: PASS. Python is the oracle. A failure here is a real Python streaming
defect and a genuine finding — record it before changing anything.

- [ ] **Step 4: Commit**

```bash
git add spec/annotation_streaming_scenarios.json tests/conformance/test_annotation_parity.py
git commit -m "test(conformance): annotation scenarios fed four different ways

Six scenarios, each run one-shot, byte-at-a-time, split at every
position, and randomly chunked from a recorded seed. All four must agree.

Split-at-every-position is the mode worth having: a detector that
retains too little suffix passes one-shot and byte-at-a-time and fails
only at the specific offset where a match straddles the boundary."
```

---

### Task 3: Each port loads the artifact and passes the scenarios

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Annotation/Detector.cs`
- Modify: the Go and TypeScript detector rule inventories (locate with the grep
  in Step 1)
- Modify: `tests/conformance/test_annotation_parity.py`

**Interfaces:**
- Consumes: `spec/annotation_rules.json`, `spec/annotation_streaming_scenarios.json`.
- Produces: nothing new.

- [ ] **Step 1: Locate each port's inventory**

Run:
```bash
cd /Volumes/data/pyv/provide-uterm
grep -rln "DetectionRule\|detectionRule" packages/provide-uterm-go packages/provide-uterm-ts/src \
  packages/provide-uterm-csharp/src --include="*.go" --include="*.ts" --include="*.cs"
```

- [ ] **Step 2: Compare each inventory against the artifact**

Before changing any detector, add a test per port asserting its rule inventory
equals `spec/annotation_rules.json` — same count, same ids, same patterns, same
order after sorting by id.

Run each port's test suite.

Expected: **this is where divergence surfaces.** The measurement did not verify
that the inventories agree, so any mismatch here is a new finding. Record what
diverged before fixing it — a rule that only one port has is either a missing
rule in three ports or an invented rule in one, and those need different fixes.

- [ ] **Step 3: Make each port load the artifact**

Prefer loading `spec/annotation_rules.json` at build or test time over keeping a
hand-maintained copy. Where a port cannot read the file at runtime (an embedded
binary, for instance), generate its inventory from the artifact and add that
generated file to the Task 1 drift check, so the copy cannot silently drift
again.

- [ ] **Step 4: Run the scenarios against all four backends**

Run: `uv run pytest tests/conformance/test_annotation_parity.py -v`

Expected: PASS on Python, Go, C# and TypeScript, in all four feed modes.

- [ ] **Step 5: Run each port's own gate**

Run:
```bash
cd packages/provide-uterm-go && go test -race ./...
cd ../provide-uterm-csharp && make quality-gate
cd ../provide-uterm-ts && npm run test:coverage
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add spec/ tests/conformance/ packages/
git commit -m "fix: every port's detector reads one rule inventory

Each port kept a hand-copied rule list and nothing checked the copies
still matched. They now load the generated artifact, or generate from it
under the same drift gate, so a rule added in Python reaches all four or
fails the build.

The streaming scenarios run against every backend in all four feed
modes, which is what proves the detectors agree rather than merely the
rule tables."
```

---

## Definition of done

Per the measurement spec, CM-06 closes when:

- `spec/annotation_rules.json` exists, is generated, and its drift check was
  observed failing against a hand edit;
- every port's rule inventory is derived from that artifact rather than copied;
- `spec/annotation_streaming_scenarios.json` passes on all four backends in all
  four feed modes;
- any inventory divergence found in Task 2, Step 2 is recorded in the
  measurement spec, whether or not it turned out to matter.

Then update the CM-06 row and the Status date in
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`.

## Notes for the implementer

- Do not change `BUILTIN_RULES` to make a port pass. If a port's behavior is
  better, that is a contract change to make deliberately in Python first.
- The retained streaming suffix must be bounded by the longest rule's
  requirement, not by an arbitrary constant. A detector that retains everything
  passes every scenario here and grows without bound in production — worth a
  separate assertion on retained-buffer size if any port's implementation makes
  that observable.
- Regex dialects differ across the four languages. If a pattern behaves
  differently in one, that is a finding about the pattern, and the fix is a
  pattern all four agree on rather than a per-port exception.
