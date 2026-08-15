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
| P2 | C# quality debt cleanup | `[x]` | Resolve high-volume analyzer warnings and low-signal anti-patterns to reduce future bug risk and review friction. | Eight compiler warnings → zero, two of which were real defects (see C# FINDINGS below), plus 28 xUnit analyzer warnings the gate had been unable to see at all. `ci/warning_gate.py` + `ci/warning-baseline.json` ratchet both, failing on a new warning and on a stale baseline entry alike. Coverage 97.44% → 97.53%. | `make -C packages/provide-uterm-csharp quality-gate` |
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

Items are listed once, `[x]` or `[ ]`, in the order they were found. Several
were opened and closed inside the same pass, which is why the section mixes
both: wiring C# to the shared golden corpus paid for itself immediately, and
`CSHARP-STATIC-001`, `CSHARP-FRAMES-001` and `CSHARP-GOLDEN-002` were all found
by it or by the full test gate rather than by review.

- [x] **CSHARP-GOLDEN-001 (P2): C# now executes a shared golden corpus.** 82
  cases over all 22 scenarios of `graphicaltargets_golden.json`, read in place
  from the TypeScript package rather than copied, so it cannot become another
  twinned fixture to drift. This is the corpus whose "a closed registry does
  nothing at all" scenario the missing `Close()` had been hiding from.
- [x] **CSHARP-STATIC-001 (P1): seeding a duplicate returned an uncoded error.**
  `AddStaticCore` threw a raw `InvalidOperationException` where the reference
  (`graphical_targets.py:433`), Go, and the corpus all give
  `CONFLICT`/"duplicate graphical target_id" — so a REST caller got an
  unhandled 500 instead of a coded refusal. Its `Validate()` call was also
  missing the `ArgumentException -> INVALID` wrapper that `CreateCore` and
  `UpdateCore` both have, so a bad seeded `target_id`, protocol or size escaped
  the same way. Found by the corpus on its first run.
- [x] **CSHARP-FRAMES-001 (P1): the C# snapshot frame was missing two wire
  fields, and its corpus copy hid that.** `SnapshotFrame` had no `bytes_read`
  or `chunks_read` — neither string occurred anywhere in the C# tree — so the
  port could not carry either ingest counter in or out. Its committed copy of
  `python_golden.json` was missing exactly those two fields, so the corpus was
  asserting the gap was correct. Both fields added to the frame and to the
  mapper's encode and decode paths (omitted when null, as Go's `omitzero` does
  and as the reference's "a worker predating them omits both" intends), and the
  copy is byte-identical with Go's again.

  The same copy had also dropped `mcp_supported`/`vnc_supported`. That one is
  *not* a gap: `spec/behavior.json` `hello_defaults.csharp` deliberately sets
  `mcp_supported: false` because MCP is de-scoped from the port. Deleting the
  keys was the wrong way to say so, because it silently took the snapshot
  counters with it. The divergence is now declared in the golden test and fails
  if it ever stops being true.
- [x] **CSHARP-GOLDEN-002 (P2): C#'s committed fixtures were outside every
  drift check.** Two independent holes, both closed. All six of C#'s Go-twinned
  corpora (plus the four-way `behavior_vectors.json` group) are now in
  `TWINNED_FIXTURES` in `scripts/check_protocol_drift.py`, where only
  `ctrlmsg/signature_corpus.json` had been; A/B'd by restoring the pre-fix
  copy, which the gate now rejects. And `.ci/check_goldens.sh` no longer stops
  at `-maxdepth 1`, so the five C# corpora that live in per-subject
  subdirectories are visible to it at all.

  Widening that find required pruning `bin/` and `obj/` in the same change: the
  C# test project copies its whole testdata tree next to the built assembly, so
  ten more "corpora" appear under `bin/Debug` and `bin/Release` after a build —
  paths that are not in the repository, which is the exact failure mode the
  script's prune list already existed for. The no-generator note also now
  separates copies (held to their source by the drift gate) from corpora that
  genuinely nothing can re-derive, because calling a copy "cannot be
  drift-checked" invites someone to write a generator for a file whose only job
  is to equal another file.

  No generator was hidden by the old depth limit — 161 at depth 1, 161 at any
  depth — so that half is a trap disarmed rather than a bug fixed.
- [ ] **CSHARP-GOLDEN-003 (P2): 104 shared corpora have a C# counterpart and
  none were consumed before this pass.** Highest value first: `pyjson`
  (`CanonicalJson` must match `json.dumps(sort_keys=True,
  separators=(",",":"))` byte for byte or every identity HMAC diverges),
  `serverauthz`, `egress`, `pattern_safety`, `hub_lease`, `managerprocess`,
  `serverauth`, `tunnelinvites`+`tokenhash`, `controlplane`.
- [ ] **CSHARP-RFB-001 (P2): endpoint parsing diverges from the corpus in 27
  cases.** `GraphicalTargetParsing` keeps the brackets on an IPv6 host
  (`"[2001:db8::1]"` rather than `"2001:db8::1"` — and that string is what a
  connection dials), reports the wrong message for a malformed port because
  `Uri.TryCreate` rejects before any port is examined, and disagrees on
  IPvFuture, zone ids and bracketed credentials.
- [ ] **CSHARP-SECHEADERS-001 (P2): no security response-header resolver.**
  `securityheaders_golden.json` has no C# counterpart and the tree has no hits
  for CSP, HSTS or `X-Content-Type-Options`.
- [ ] **CSHARP-XUNIT-001 (P2): 28 baselined xUnit analyzer warnings.** They
  were invisible until `ci/warning_gate.py`'s code pattern was widened to
  accept a lower-case analyzer prefix; the gate had been reporting
  "0 warning(s)" over a build that had 28. Now baselined per file, so the
  backlog can shrink but not grow. Mostly `xUnit1031` (blocking task operations
  in a test method), fixed by making each call site async.
- [x] **CSHARP-APPROVAL-002 (P1): the C# approvals subsystem was unreachable
  from production traffic.** C# had no policy gate on the browser input path,
  so nothing but a test ever created an approval request, and `HandleApprove`
  had no `ResolveApproval` equivalent — it claimed the request and returned 200
  without injecting the held command, so it could never answer the 409 Python
  and Go return on refused delivery. The store fix made it correct; it did not
  make the feature real.

  Go's hold path is now ported: `IInputPolicyGate` with a no-op default (so an
  ungated deployment is byte-for-byte unchanged), parked browsers with bounded
  hold buffers, ownership re-validation on park, the `approval_pending`
  broadcast, one-shot revision claim, buffer replay on approve and discard on
  reject, and the refusal case where the owner loses the lease between decision
  and injection — `outcome: "refused"` and REST 409, which C# previously could
  not produce at all. The three frame types C# had defined and never sent are
  now sent.

  Non-vacuity: reverting only the input seam turns 7 of 10 integration tests
  red. The 3 that stay green are the no-gate default and the two sweep tests,
  which drive the hub directly.
- [x] **CSHARP-APPROVAL-003 (P2): no approval sweep.** A 30-second
  `CleanupExpired` sweep now runs with the server, and `OnExpired` releases the
  parked browser rather than leaving it held with its input buffered.

Closed in this pass, kept here only as pointers to where the answer landed:

- [x] **DOC-TUNNEL-001 (P2)** — both documents were true about different
  things. Cloudflare *does* mount `/tunnel/{worker_id}`; what it lacks is the
  mux. `docs/security-language-parity.md`'s flat denial was the wrong half, and
  row 11 of `docs/cloudflare-divergence-matrix.md` overstated it in the other
  direction. The `unserved` label on the fragmentation row stands.
- [x] **DOC-COUNT-001 (P3)** — 28 is the real count and the README already
  enforced it; two independent off-by-ones elsewhere, one of them a doc
  miscounting its own sub-list rather than a TS-only extra tool.
- [x] **DOC-FANOUT-001 (P3)** — neither `unserved` nor `N/A`: the REST routes
  are mounted and the browser socket has no `fanout_send` case at all, so the
  honest label is the words `N — not implemented (REST only)`.

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
