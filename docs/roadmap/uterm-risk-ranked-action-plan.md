# uterm Risk-Ranked Action Plan (Living)

Date: 2026-08-15  
Branch: `main` (`origin/main`)  
Owner of this document: Architecture + release owners

Purpose
- Provide a stable, actionable plan for reducing production risk across all implementations.
- Keep scope explicit for each language/backend.
- Keep this document current by updating status, owners, and completion criteria as work is done.

## Plan status legend

- **P0**: Must fix before release or parity claim
- **P1**: High confidence impact, should be in next hardening cycle
- **P2**: Important quality/risk guardrails
- **P3**: Improvement for operational consistency and observability

- `[ ]` not started
- `[~]` in progress
- `[x]` completed

## TS scope decision (committed)

- **Decision (2026-08-15): keep TypeScript as **partial backend only**.**
- **Reasoning:** TypeScript has strong protocol, client, and partial server libraries, but is not yet a parity replacement for Python/Go/C# in lifecycle + WS + full route coverage.
- **Enforcement:** TypeScript is listed as `N`/unserved for unsupported protocol cells, and CI/live matrix jobs continue to run the served set as Python/Go/C# only.
- **Impact:** We will not advertise TypeScript as a full served backend until this scope is closed.

## Risk-ranked action matrix

| Rank | Area | Description | Owner | Dependencies | Evidence to capture | Acceptance criteria | Command(s) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P0 | TS backend parity scope and contract | Enforce TypeScript as partial (no full backend parity claim): complete route/transport module coverage remains explicit and unserved. | ADR / product owner + TS core owner | `docs/protocol-matrix.md`, `docs/feature-roadmap.md`, package exports | Signed decision doc + matrix evidence (unsupported cells where expected) | TypeScript remains unserved for full parity cells with explicit unsupported status, and this is documented in matrix/docs and enforced in CI selector lists | `npm run lint --workspace=packages/provide-uterm-ts`<br>`npm run build --workspace=packages/provide-uterm-ts`<br>`python conformance/live/harness --list-drivers` |
| P1 | Lifecycle/fanout race hardening | Expand lifecycle and fanout stress tests where ownership, resume, pause, and delivery races are most failure-prone. | Python + Go + C# test owners, with Cloudflare representative tests | Existing conformance scenario files and cross-language harness | New failing repro tests that now pass; improved coverage in timing/ordering edges | Red-team timing tests for attach/detach/reconnect and delivery races executed in CI and stable across languages | `GOWORK=off uv run pytest -q tests/conformance/live/test_matrix.py`<br>`cd packages/provide-uterm-go && GOWORK=off go test ./...`<br>`cd packages/provide-uterm-csharp && dotnet test --no-restore`<br>`cd packages/provide-uterm-cloudflare && uv run pytest -q tests/test_security.py::test_*` |
| P1 | Protocol drift guardrails | Prevent behavior drift by enforcing protocol/spec + fixture + docs checks on any change touching wire contracts. | Platform/runtime owners + CI owner | `spec/`, `spec/*_corpus.json`, docs protocol matrix | Diff checks and a PR checklist for protocol changes | CI fails on stale matrix/docs when protocol changes; every protocol commit updates corpus/tests | `uv run python scripts/run_session_lifecycle_security_scenarios.py --validate-only`<br>`uv run python scripts/run_fanout_security_scenarios.py --validate-only` |
| P1 | Cloudflare behavior parity boundaries | Codify and continuously test Cloudflare-specific intentional divergences from FastAPI/Go/C# behavior. | Cloudflare owner + architecture owner | `docs/operations/*.md`, ARD notes, conformance harness | Explicit doc matrix row for each divergence + tests that assert edge behavior | Any change to shared protocol touching edge-runtime behavior must update divergence table and edge tests | `cd packages/provide-uterm-cloudflare && uv run pytest -q tests/conformance` |
| P2 | C# quality debt cleanup | Resolve high-volume analyzer warnings and low-signal anti-patterns to reduce future bug risk and review friction. | C# core owner | Current warning list, `dotnet` test output | Warning baseline tracked in CI/artifact | Reduced warning count; no new warning class introduced by PRs | `cd packages/provide-uterm-csharp && dotnet test --no-restore --verbosity minimal` |
| P2 | Benchmark reproducibility and comparability | Standardize benchmark commands, warmup, sample sizing, and environment constraints to avoid local-machine bias. | SRE/performance owner + each language owner | Benchmark scripts in root/scripts or CI workflows | Shared command matrix with reproducible results logs | Same benchmark harness produces stable deltas across CI retries and commit history | `make -f Makefile.bench bench-local` *(to be created)*<br>`make -f Makefile.bench bench-ci` *(to be created)* |
| P3 | Documentation and review hygiene | Keep roadmap, remediation status, and implementation matrices in sync after each language/backend change. | Docs owner + release owner | `docs/roadmap/uterm-code-review-remediation.md`, `docs/ARCHITECTURE.md` | Auto-sync checklist and review checkpoint updates on each merge | 100% of parity-related PRs update docs; stale claims removed within 1 review cycle | `rg -n "served|parity|unsupported" docs packages -S`<br>Targeted doc review in PR checklist |

## 90-day execution plan

### Week 1–2
- Finalize TS parity scope with ownership signoff (completed).
- Add/lock CI checks that enforce the chosen TS scope (already aligned: live matrix excludes TS from served server set).
- Publish first revision of divergence matrix for Cloudflare.

### Week 2–5
- Add at least 6 new lifecycle/fanout race tests in the Python/Go/C# harnesses.
- Add TS/CF equivalents where behavior is intended.
- Run full conformance matrix against all served implementations.

### Week 4–7
- Add protocol-drift check to CI gate (or fail-fast script) in repo workflows.
- Require protocol change checklist in PR template for any `spec/` diff.

### Week 6–8
- Establish benchmark harness and run baseline capture on all languages.
- Add benchmark report template and historical artifact path (`docs/benchmarks/` or CI artifacts).

### Week 8–10
- Resolve first wave of C# warnings and track remaining technical debt.
- Clean up docs/roadmap links and mark parity scope in one pass.

## Execution roles and cadence

- **Week cadence:** execute one risk column per sprint block.
- **Review cadence:**  
  - Weekly: one risk item status update.  
  - Monthly: hard-priority reclassification if risk behavior changed.  
- **Completion criteria:**  
  - Every P0/P1 item includes commands + evidence + tests in PRs.  
  - No new "served parity" claim without implementation + harness updates.

## Backlog of "ready next" tasks

- [x] TS-DECIDE-001: Publish parity decision and enforce via CI matrix/docs.
- [ ] P1-LIFECYCLE-001: Add race matrix cases for owner handoff and stale approval expiry.
- [ ] P1-PROTO-001: Add protocol version and fixture drift checks to CI.
- [ ] P1-CF-001: Add/refresh Cloudflare divergence matrix and edge-only regression tests.
- [ ] P2-CSHARP-QUALITY-001: Address warning clusters from `dotnet test` output.
- [ ] P2-BENCH-001: Create repeatable benchmark scripts/containers for cross-language comparison.
- [ ] P3-DOCS-001: Consolidate parity labels in `README` and server docs.

## Maintenance checklist for this plan (run on each major merge)

- Update owner, status, and ETA on changed items.
- Add evidence links to each completed check (`command output`, `PR`, `artifact`).
- Move stale tasks to archive or mark deferred with rationale.
- Confirm no task drift between:
  - `docs/roadmap/uterm-code-review-remediation.md`
  - `docs/protocol-matrix.md`
  - `docs/ARCHITECTURE.md`
