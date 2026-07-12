# Go Full-Module Mutation Gate Design

## Goal

Mutation-test every eligible production package in `packages/provide-uterm-go` and require an effective score of 100%: every viable, covered mutant must be killed or documented as behaviorally equivalent. The gate must run reproducibly in CI and locally, and adding a new Go package must automatically add it to the mutation perimeter.

## Current State

The existing `ci/mutation_gate.py` runs Gremlins v0.6.0 against seven hand-maintained packages. The main CI job invokes that small perimeter serially. This gives a strong signal for those packages but does not support a module-wide mutation claim.

Gremlins runs tests at package scope. Its documentation notes that package-local execution does not catch mutations whose only observable effect is in another package's tests. Therefore the design uses package-local mutation for the exhaustive matrix and retains full-module tests as a separate prerequisite; cross-package behavioral contracts remain covered by ordinary integration and conformance tests.

## Eligible Surface

The perimeter is discovered from `go list ./...`, not a hand-maintained package tuple. Every package containing non-test Go source is included, including command packages and integration packages.

Only files in these categories may be excluded:

- generated lookup tables whose headers or repository metadata identify them as generated;
- `doc.go` files containing documentation only;
- platform-specific files that cannot compile on the CI platform, while the package's files for the active platform remain included;
- mechanically generated mocks, if any are added later.

Exclusions live in a version-controlled TOML file. Every entry contains the file glob and a concrete reason. The matrix generator fails when an exclusion matches no file, preventing stale exclusions.

## Architecture

### Package discovery

A new `ci/mutation_matrix.py` runs `go list -json ./...` from the Go module root and emits a JSON array of relative package paths. It rejects an empty matrix and verifies that every discovered production package is either included or explicitly excluded.

The script supports `--json` for GitHub Actions and newline output for local use. Unit tests exercise package parsing, exclusions, empty discovery, and deterministic ordering without invoking the network.

### Per-package gate

`ci/mutation_gate.py` changes from a fixed serial perimeter to a single-package runner:

```text
python3 ci/mutation_gate.py --package ./server
```

It invokes the pinned Gremlins version, parses JSON output, and fails on `LIVED`, `NOT_COVERED`, or `TIMED_OUT`. `NOT_VIABLE` remains non-fatal because the mutant does not compile. A `LIVED` mutant passes only when its exact package, file, line, column, and mutator key appears in `mutation_equivalents.toml` with a substantive reason.

The runner fails on stale equivalent-mutant entries for the package being checked. This is stricter than the current warning and keeps the effective 100% score auditable.

### Aggregate local runner

`ci/mutation_all.py` discovers the matrix and runs every package, with configurable bounded parallelism. It writes one report per package beneath `mutation-results/` and a combined JSON summary containing killed, equivalent, not-viable, uncovered, timed-out, and surviving counts.

`make mutation-gate` runs the complete module. `make mutation-package PACKAGE=./server` provides a focused development loop.

### CI workflow

The main workflow gains a discovery job and a matrix mutation job. Each matrix cell runs one complete package mutation gate. The matrix uses `fail-fast: false` so one run reports every deficient package. A final aggregation job downloads reports, verifies that every discovered package produced a report, and enforces an effective score of 100%.

The full matrix runs for every pull request and push to `main`, as requested. Concurrency is capped to control runner pressure, but no package is skipped. Results are uploaded as artifacts for survivor triage.

## Ratchet and Completion Policy

The implementation begins by measuring all packages. Failures are triaged into three categories:

1. missing coverage or a surviving behavioral mutant: add a focused test;
2. genuinely equivalent mutation: add a reviewed exact-key allowlist entry with proof;
3. tool or timeout defect: fix the runner or timeout coefficient; never excuse it as equivalent.

The branch is not ready to merge until the aggregate effective score is 100%. There is no permanent sub-100 baseline. The ratchet prevents regression during development by recording the measured per-package score, but the required merge gate is raised to 100% before integration.

## Error Handling

The gate fails closed when Gremlins emits no JSON, package discovery is empty, a subprocess exits unexpectedly, a package report is missing, an exclusion is stale, an equivalent entry is stale, or a mutant is uncovered/timed out. Logs include the exact reproduction command for every failing package.

## Testing

- Unit-test matrix discovery and exclusion validation using synthetic `go list -json` streams.
- Unit-test all Gremlins status classifications and exact equivalent matching.
- Verify the aggregate runner detects missing and duplicate reports.
- Run the existing seven-package gate before and after refactoring to prove compatibility.
- Run the full discovered matrix and add mutation-killing tests until the effective score reaches 100%.
- Run `go test ./...`, `go test -race ./...`, the coverage gate, `go vet`, and lint after mutation work.

## Non-Goals

- Python and C# mutation expansion are separate tasks.
- Mutation testing does not replace fuzzing, race detection, vulnerability scanning, or ordinary coverage gates.
- Equivalent-mutant entries are not a mechanism for accepting hard-to-test behavior.
