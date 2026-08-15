# uterm Risk-Ranked Action Plan (Living)

- Date: 2026-08-15
- Branch: `main` (`origin/main`)
- Owner of this document: Architecture + release owners

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

- **Decision (2026-08-15): keep TypeScript as partial backend only.**
- **Reasoning:** TypeScript has strong protocol, client, and partial server libraries, but is not yet a parity replacement for Python/Go/C# in lifecycle + WS + full route coverage.
- **Enforcement:** TypeScript is listed as `N`/unserved for unsupported protocol cells, and CI/live matrix jobs continue to run the served set as Python/Go/C# only.
- **Impact:** We will not advertise TypeScript as a full served backend until this scope is closed.
- **Vocabulary:** `docs/parity-labels.md` now defines `served` / `unserved` / `unsupported` / `partial` / `N/A` once, and every parity table links to it. The PR template's protocol checklist restates the rule that a TS cell may not be marked `served` without a mounted server surface.

## Risk-ranked action matrix

| Rank | Area | Status | Description | Evidence captured | Command(s) |
| --- | --- | --- | --- | --- | --- |
| P0 | TS backend parity scope and contract | `[x]` | Enforce TypeScript as partial (no full backend parity claim): complete route/transport module coverage remains explicit and unserved. | Decision restated in `docs/parity-labels.md` with the served set named (Python, Go, C#, Cloudflare); root and TS READMEs corrected from "not yet a full backend" to "not a served backend"; the two shared contracts pin `typescript` to `unserved` and their validators refuse any other status. | `uv run python scripts/run_session_lifecycle_security_scenarios.py --validate-only`<br>`uv run python scripts/run_fanout_security_scenarios.py --validate-only` |
| P1 | Lifecycle/fanout race hardening | `[x]` | Expand lifecycle and fanout stress tests where ownership, resume, pause, and delivery races are most failure-prone. | Two new race categories in `spec/session_lifecycle_security_scenarios.json` (10 → 12 scenarios), executed natively by Python, Go, C#, and Cloudflare. Found a real defect: see LIFECYCLE FINDING below. | `uv run python scripts/run_session_lifecycle_security_scenarios.py` |
| P1 | Protocol drift guardrails | `[x]` | Prevent behavior drift by enforcing protocol/spec + fixture + docs checks on any change touching wire contracts. | `scripts/check_protocol_drift.py` in the static gate; the two contract validations wired in beside it; `.github/pull_request_template.md` carries the protocol checklist; `tests/scripts/test_check_protocol_drift.py` proves the gate is not vacuous. | `bash ci/quality_checks.sh`<br>`uv run python scripts/check_protocol_drift.py --changed-against origin/main` |
| P1 | Cloudflare behavior parity boundaries | `[x]` | Codify and continuously test Cloudflare-specific intentional divergences from FastAPI/Go/C# behavior. | `docs/cloudflare-divergence-matrix.md`: 11 rows, each with intent, `file:line` evidence, and a pinning test. 23 new offline tests in `tests/test_edge_divergence_matrix.py`. Found a stale doc claim: see DOC FINDING below. | `uv run --frozen --package provide-uterm-cloudflare --extra dev pytest -q packages/provide-uterm-cloudflare/tests` |
| P2 | C# quality debt cleanup | `[x]` | Resolve high-volume analyzer warnings and low-signal anti-patterns to reduce future bug risk and review friction. | Eight warnings → zero, two of which were real defects (see C# FINDINGS below). `ci/warning_gate.py` + an empty `ci/warning-baseline.json` ratchet it, failing on both a new warning and a stale baseline entry. Coverage 97.44% → 97.48%. | `make -C packages/provide-uterm-csharp quality-gate` |
| P2 | Benchmark reproducibility and comparability | `[x]` | Standardize benchmark commands, warmup, sample sizing, and environment constraints to avoid local-machine bias. | `Makefile.bench` declares every parameter once and exposes two profiles; `scripts/bench_env_fingerprint.py` writes the machine beside every result; `docs/benchmarks/README.md` states what makes two numbers comparable. | `make -f Makefile.bench bench-local`<br>`make -f Makefile.bench bench-ci` |
| P3 | Documentation and review hygiene | `[x]` | Keep roadmap, remediation status, and implementation matrices in sync after each language/backend change. | `docs/parity-labels.md` is the single vocabulary; eight documents normalized onto it; the PR template makes the doc update a checklist item rather than a convention. | `uv run python scripts/check_docs_accuracy.py`<br>`rg -n "served\|parity\|unsupported" docs packages -S` |

## Findings from the 2026-08-15 hardening pass

These were not on the plan. Each was found by doing a plan item, which is the argument for the guardrails the items added.

**LIFECYCLE FINDING — C# approvals never expired (fixed).** Adding the
`approval_expiry_refuses_late_claim` race case turned the C# cell red on first
run: `POST /api/approvals/{id}/approve` returned `200 approved` for a request
whose deadline had passed by ~999 seconds, and delivered the held command.
`InMemoryApprovalStore.CleanupExpired()` was the only code that could retire a
deadline and **nothing in production called it** — Go ticks it from
`StartSweeps`, Python from `sweep_expired_approvals`, and the C# port has no
sweep at all. Unlike Python's `claim_request`, C#'s `Claim` also had no inline
deadline re-check, so there was no second line of defence. Fixed by checking the
deadline on every read and write path, which is what the reference does inline.
The scenario is the A/B: it was red before the fix and green after, with no
change to the assertion.

**DOC FINDING — the protocol matrix described a refusal that does not exist
(fixed).** `docs/protocol-matrix.md` advertised Cloudflare as
`hijack_control=rest`, refusing `hijack_request` / `hijack_release` /
`hijack_step` with `use_rest_hijack_api`. The Worker emits `"ws"` from all three
of its hello paths and serves all three frames; `use_rest_hijack_api` occurred
nowhere in the tree except that table. The Worker had reached WS-hijack parity
and only the doc still disagreed — dangerous in that direction, because the
obvious way to resolve the drift is to regress the Worker to match the doc.

**C# FINDINGS — two warnings were defects, not noise (fixed).**
`CS0649` on `InMemoryGraphicalTargetRegistry._closed` was the only signal that
the port carried the closed-state guard but never the `Close()` that sets it,
leaving `GraphicalTargetErrorCode.Closed` unreachable while Python and
TypeScript both expose `close()`. `CS0414` on `MemoryEngine._open` advertised a
closed-state guard the reference deliberately does not have. Neither was
reachable by a test, which is exactly why the compiler was the only witness.

**CI FINDINGS — two gates were already failing on `main` (fixed).**
`docs-quality` was red because this document used markdown hard line breaks and
`check_docs_accuracy.py` rejects trailing whitespace. The C# `build-binaries`
stage failed with `NETSDK1102` after an SDK update, because `PublishAot` and
`PublishTrimmed` cannot apply to the framework-dependent publish the Makefile
performs — so the C# gate could not reach its final stage regardless of what
the suite did.

## Backlog of "ready next" tasks

- [x] TS-DECIDE-001: Publish parity decision and enforce via CI matrix/docs.
- [x] P1-LIFECYCLE-001: Add race matrix cases for owner handoff and stale approval expiry.
- [x] P1-PROTO-001: Add protocol version and fixture drift checks to CI.
- [x] P1-CF-001: Add/refresh Cloudflare divergence matrix and edge-only regression tests.
- [x] P2-CSHARP-QUALITY-001: Address warning clusters from `dotnet test` output.
- [x] P2-BENCH-001: Create repeatable benchmark scripts/containers for cross-language comparison.
- [x] P3-DOCS-001: Consolidate parity labels in `README` and server docs.

### Opened by this pass

- [ ] **CSHARP-APPROVAL-002 (P1): the C# approvals subsystem is unreachable from production traffic.** C# has no policy gate on the browser input path (Python's `_policy_gate.intercept_input` `hold` branch), so nothing but a test ever creates an approval request. `HandleApprove` also has no `ResolveApproval` equivalent: it claims the request and returns 200 without injecting the held command, so it can never answer the 409 that Python and Go return when delivery is refused. The expiry fix makes the store correct; it does not make the feature real. Decide whether C# implements the hold path or declares approvals explicitly unsupported.
- [ ] **CSHARP-APPROVAL-003 (P2): no approval sweep.** Expiry is now checked on every read and write, which is what the contract needed, but without a background sweep an `OnExpired` notification only fires when someone touches the store. Harmless while approvals are REST-only (above); wire a sweep if the hold path lands.
- [ ] **CSHARP-GOLDEN-001 (P2): C# does not consume the shared golden corpora.** `graphicaltargets_golden.json` has always contained the scenario "a closed registry does nothing at all", and Python and TypeScript both execute it. C# is the one port that does not, which is why its missing `Close()` survived. Wire the C# port into the golden corpora it has counterparts for.
- [ ] **DOC-TUNNEL-001 (P2): contradictory Cloudflare tunnel claims.** `docs/security-language-parity.md` says Cloudflare has no tunnel WebSocket route (`unserved`); `docs/protocol-matrix.md` gives Cloudflare a `WSS /tunnel/{tunnel_id}` agent endpoint. One is wrong, and if the route genuinely does not exist the label should be `N/A`.
- [ ] **DOC-COUNT-001 (P3): stale MCP tool counts.** `docs/feature-roadmap.md` says "tool 21 of 21"; there are 28 `@mcp.tool` decorators, which is what the README and `check_docs_accuracy.py` use. `docs/typescript-port-roadmap.md` names 28 reference tools and claims twenty-nine ported bodies.
- [ ] **DOC-FANOUT-001 (P3): unqualified cell.** "Browser-WS fan-out send served" is a bare `N` for C#, which does not distinguish an unmounted module (`unserved`) from nothing implemented.

## Execution roles and cadence

- **Week cadence:** execute one risk column per sprint block.
- **Review cadence:**
  - Weekly: one risk item status update.
  - Monthly: hard-priority reclassification if risk behavior changed.
- **Completion criteria:**
  - Every P0/P1 item includes commands + evidence + tests in PRs.
  - No new "served parity" claim without implementation + harness updates.

## Maintenance checklist for this plan (run on each major merge)

- Update owner, status, and ETA on changed items.
- Add evidence links to each completed check (`command output`, `PR`, `artifact`).
- Move stale tasks to archive or mark deferred with rationale.
- Confirm no task drift between:
  - `docs/roadmap/uterm-code-review-remediation.md`
  - `docs/protocol-matrix.md`
  - `docs/parity-labels.md`
  - `docs/cloudflare-divergence-matrix.md`
  - `docs/ARCHITECTURE.md`
