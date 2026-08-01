# uterm Code Review Remediation Tracker

This tracker records the actionable findings from the 2026-07-31 comprehensive
architecture and implementation review. Status is evidence-based:

- `[ ]` pending
- `[~]` implementation or review in progress
- `[x]` focused tests and the relevant quality gate pass

Design: `docs/superpowers/specs/2026-07-31-code-review-remediation-design.md`

Architecture and implementation report:
`docs/reports/2026-07-31-uterm-comprehensive-code-review.md`

## Cross-language fan-out security

- [x] **FANOUT-001 (high): configurable unknown-member admission.** Python, Go,
  C#, and TypeScript reject unknown worker IDs by default; one consistently named
  option permits dormant IDs. Acceptance: strict/default and permissive tests exist
  in each served implementation.
- [x] **FANOUT-002 (high): send-time session authorization.** Every group member is
  resolved and authorized on every send and approval release. Unknown or revoked
  members receive a failure and no input. Group grants do not bypass session authz.
- [x] **FANOUT-003 (high): governance policy enforcement.** Python factory wiring
  supplies a real fan-out policy adapter and fails closed when configured policy is
  unavailable. Go and C# cannot silently bypass configured governance.
- [x] **FANOUT-004 (medium): C# semantic parity.** Parallel/sequential execution,
  response collection, elapsed time, stop-on-first-error, and divergence behave as
  advertised or the surface explicitly reports unsupported behavior.
- [x] **FANOUT-005 (high): shared parity coverage.** Cross-language behavioral
  scenarios cover unknown IDs, revoked authorization, group grants, policy deny,
  policy hold/release, partial member failures, capture lifecycle, store safety,
  and operation deadlines. The contract is
  `spec/fanout_security_scenarios.json`; native adapters are executed and
  compared by `scripts/run_fanout_security_scenarios.py`.
- [x] **FANOUT-006 (high): isolated and atomic in-memory stores.** Python and
  TypeScript stores copy mutable records at save/read/list boundaries, and grants
  use a store-owned atomic update rather than a detached read/modify/write cycle.
- [x] **TS-APPROVAL-ABA-001 (high): held fan-out state is revision-bound.**
  TypeScript carries an opaque store-assigned revision through pending payload,
  expiry callback, and release. A delayed timeout for a pruned/reused request ID
  cannot delete or release the newer command.
- [x] **FANOUT-RUNNER-TIMEOUT-001 (high): native adapters are bounded.** The
  central fan-out runner applies a finite per-backend subprocess timeout, catches
  expiration, reports the backend clearly, and fails closed without accepting
  partial observations. Deterministic runner tests cannot hang indefinitely.
  The 120-second default bound is pinned by a focused conformance test.

## C# WebSocket and connection lifecycle

- [x] **CSHARP-WS-001 (high): fragmented browser messages.** Browser control and
  input messages are accumulated through `EndOfMessage` with a bounded size.
- [x] **CSHARP-WS-002 (high): fragmented worker messages.** Worker snapshots and
  control frames retain decoder state across receive fragments.
- [x] **CSHARP-WS-003 (high): fragmented tunnel messages.** Tunnel frames are
  decoded only after a complete bounded WebSocket message is assembled.
- [x] **CSHARP-CONN-001 (medium): worker admission.** Both worker and tunnel routes
  honor failed `RegisterWorker` results and do not mark rejected workers online.
- [x] **CSHARP-CONN-002 (medium): browser quotas.** Enforce
  `MaxConnectionsPerPrincipal`, including rollback on setup and disconnect.
- [x] **CSHARP-CONN-003 (medium): handshake ordering.** Deferred browser activation
  prevents normal broadcasts before `hello` and initial state frames.
- [x] **CSHARP-CONN-004 (medium): bounded broadcast.** Browser sends are concurrent
  or isolated, have a timeout, and prune failed sockets.
- [x] **CSHARP-RESUME-001 (medium): bounded token storage.** Expired or abandoned
  resume tokens are swept and total storage is capped.
- [x] **CSHARP-RESUME-002 (medium): truthful resume semantics.** Successful resume
  restores documented ownership/state; otherwise the capability is not advertised.
- [x] **CSHARP-AUTH-001 (low): constant-time worker credential comparison.** Worker
  and tunnel bearer validation use a constant-time byte comparison.
- [x] **CSHARP-FANOUT-INIT-001 (medium): publication-safe lazy controller.**
  Concurrent first use creates exactly one default controller/store and cannot
  strand groups in split instances.
- [x] **CSHARP-REST-STEP-001 (high): truthful step delivery.** Failed worker
  delivery returns conflict with no success effects; successful delivery records
  its event/metric and reports lease expiry.
- [x] **CSHARP-WIRE-001 (high): authenticated control bytes are preserved.**
  Authentication inspects the exact control-channel bytes without lossy text
  reconstruction; differential wire tests pin the accepted and rejected frames.
- [x] **CSHARP-ADMISSION-001 (high): unknown sessions and test mode fail closed.**
  Browser upgrades require a configured session outside explicitly scoped test
  mode. Test helpers restore `UTERM_TEST_MODE` and cannot contaminate later
  authorization tests.
- [x] **CSHARP-PEER-001 (medium): stale browser isolation and bounded close.**
  Failed/obsolete viewers are pruned, close acknowledgements are bounded, and a
  slow stale peer cannot block active browsers or teardown.
- [x] **CSHARP-LEASE-001 (high): serialized pause/resume settlement.** Explicit
  and forced release, disconnect, expiry, and compensation carry and settle the
  exact pause obligation without ghost ownership or a stale resume crossing a
  successor acquisition.
- [x] **CSHARP-LEASE-002 (high): fair bounded input/transition reservations.**
  Authorized input is FIFO, lease transitions cannot starve behind queued input,
  cleared waiters are cancelled, replacement order is preserved, and worker sends
  have a deadline.
- [x] **CSHARP-WORKER-001 (high): worker identity and teardown fencing.** Worker
  replacement rejects displaced frames, reconciles teardown exactly once, and
  cannot strand inherited lease state.
- [x] **CSHARP-PUBLICATION-001 (high): ownership publication is current.**
  Dashboard acquire/release/disconnect and resume-failure paths publish only the
  settled current generation; stale loss notifications cannot overwrite it.
- [x] **CSHARP-COV-001 (medium): restore the native quality-gate floor.** The
  Makefile now executes the previously omitted lifecycle/fan-out tests and binds
  the remaining hub/lease/connection branches. The serial gate runs 1,665 tests,
  reaches 97.44% against the retained 97.4% floor, and validates both release
  binaries.

## TypeScript readiness and CI

- [x] **TS-BUILD-001 (high): emit build.** `npm run build --workspace=packages/provide-uterm-ts`
  succeeds with the intended ES2023 library declarations.
- [x] **TS-CI-001 (high): build gate.** CI invokes the TypeScript emit build in
  addition to typecheck, lint, and coverage.
- [x] **TS-COV-001 (medium): restore strict coverage.** Focused tests cover the
  empty normalized Cloudflare team-domain branch and all current branch gaps;
  the 100% threshold passes without exclusions.
- [x] **TS-DOC-001 (medium): accurate maturity labels.** README, roadmap, capability
  declarations, and CI matrices consistently distinguish completed modules from
  integrated server surfaces.
- [x] **TS-E2E-001 (medium): integration eligibility.** Document the concrete
  WebSocket/lifecycle prerequisites for joining multi-backend Playwright testing.

## Native capture

- [x] **NATIVE-001 (medium): nonblocking bounded delivery.** Capture consumer
  backpressure cannot block intercepted application I/O.
- [x] **NATIVE-002 (medium): complete serialized writes.** Short writes, `EINTR`,
  concurrency, and disconnects cannot corrupt subsequent frame boundaries.
- [x] **NATIVE-003 (medium): payload bounds.** Length conversion and allocation are
  checked before building a frame.
- [x] **NATIVE-004 (medium): automated native coverage.** Deterministic tests cover
  framing/backpressure behavior; CI builds or tests the native targets where viable.

## Browser frontend and application quality

- [x] **FRONTEND-COV-001 (high): make the configured frontend coverage gate real.**
  Direct DeckMux and browser-runtime tests bring the final 556-test suite to
  94.22/85.89/92.94/95.73%, above the retained 90/85/90/90 thresholds; only
  explicit side-effect-only bootstrap exclusions remain.
- [x] **APP-COV-001 (medium): gate the React application's operational views.**
  Connect, inspect, operator, replay, and session views are exercised by 369
  tests; the application enforces 90/80/90/90 and reaches
  94.18/81.27/96.04/96.98%.
- [x] **WEB-LINT-001 (medium): make browser lint signal actionable.** Frontend and
  application lint is warning-free and CI invokes both workspaces with warnings
  treated as errors.

## Python robustness and quality

- [x] **PY-PAM-001 (low): non-object JSON.** PAM listener rejects arrays, strings,
  numbers, booleans, and null without terminating the connection handler.
- [x] **PY-GRAPH-001 (low): endpoint parser errors.** Malformed bracketed IPv6 and
  related URL parser errors become `GraphicalTargetError` with a client-error shape.
- [x] **PY-CF-001 (low): invalid JSON body.** Cloudflare request decoding handles
  `JSONDecodeError` consistently with other invalid request bodies.
- [x] **PY-TEST-001 (medium): thread cleanup.** The VNC relay regression test emits
  no `PytestUnhandledThreadExceptionWarning`.
- [x] **PY-COV-001 (medium): restore server coverage gate.** The normalized-empty
  Cloudflare team-domain path is tested and server coverage returns to 100%.
- [x] **PY-GOV-001 (high): completed-command governance is fail closed.** Deny and
  unavailable decisions never reach the worker; only an explicit allow proceeds.
- [x] **PY-RESUME-TOKEN-001 (high): rejected resume preserves legitimate
  authority.** Competing-owner and other failed resume paths do not burn the
  single-use token before all authority gates pass. Concurrent replay still has
  one winner, and any pause/ownership acquired before a losing token settlement
  is generation-bound and exactly compensated.
- [x] **PY-E2E-001 (medium): strict-admission regressions remain diagnostic.**
  The max-group-size public-route test uses 60 registered sessions instead of
  60 unknown IDs, so default-reject admission cannot mask size enforcement.
  WebSocket delivery-failure mocks accept the production delivery-fence
  keywords and fail deterministically instead of hanging on a receive.

## Cross-language lifecycle security

- [x] **LIFECYCLE-FENCE-001 (high): authorize-to-deliver ownership fence.**
  Python, Go, and Cloudflare serialize the final ownership/authorization check
  with browser input, REST send/step, and worker delivery. Release, expiry,
  acquisition/replacement, dead-owner cleanup, disconnect, and worker
  replacement cannot cross an in-flight reserved delivery. External sends are
  bounded and deterministic release/expiry/replacement races cover every source.
- [x] **APPROVAL-DELIVERY-001 (high): approval release is reauthorized and
  truthful.** Held commands and buffered replay retain their origin browser and
  ownership generation. Approval cannot dispatch stale authority after an ABA
  release/reacquire or duplicate request-ID replacement. Approval-store reads are
  immutable snapshots, terminal writes target the claimed revision, and the
  approved command plus buffered replay cannot be overtaken by fresh input. Its
  HTTP/status result distinguishes refused delivery from a command that already
  executed before a replay failure.
- [x] **APPROVAL-EXPIRY-001 (high): expiry is an atomic lifecycle decision.**
  An already-expired request cannot be claimed during the sweep interval.
  Timeout carries the immutable request revision through composed subscribers,
  clears the exact origin browser's paused/buffered state, and cannot be hidden
  by fan-out callback registration.
- [x] **FANOUT-APPROVAL-001 (high): fan-out approval status is delivery-aware.**
  A missing controller, missing pending command, authorization refusal, member
  error, or partial delivery cannot finalize or report unconditional success;
  HTTP responses preserve the normalized refusal/partial detail. Pending fan-out
  payloads are revision-bound so a delayed timeout notification for a pruned and
  reused request ID cannot delete the newer approval's execution state.
- [x] **CF-LIFECYCLE-EVIDENCE-001 (high): edge-native capability evidence.**
  Every claimed Cloudflare lifecycle cell is bootstrapped through a public
  identity-bound route and executed on the pinned local edge runtime. Explicitly
  unsupported quota/governance cells return an observable refusal; runtime,
  JWT/JWKS, worker-token, Node-version, timeout, and skip handling fail closed in
  the central runner and CI. Worker generations reject displaced frames and
  teardown, active pause state survives replacement, stable browser ownership is
  rebuilt after real hibernation, display labels are separate from authenticated
  heartbeat identity, all request validation precedes input side effects, and a
  disabled resume feature is not advertised.
- [x] **CF-REGEX-001 (high): expectation regexes fail closed before delivery.**
  The edge validator rejects unsafe grammar, including omitted-lower counted
  quantifiers (`{,}` and `{,m}`), sequential variable quantifiers, backreferences,
  alternation, and lookaround. Public-route tests prove zero worker frames.
- [x] **CF-RESUME-EXPIRY-001 (high): resumed ownership survives awaited pause
  truthfully.** A supported one-second lease may expire while a successful worker
  pause is in flight. Resume revalidates after that await, settles worker/owner
  state, and rejects instead of dereferencing or advertising an expired session.
  Deterministic delayed-success coverage binds the race.
- [x] **CF-COV-001 (medium): the declared Cloudflare coverage gate is real.**
  The package's strict full-suite command must reach its configured 100% statement
  and branch threshold; the independently reproduced 97.06% baseline is not a
  release pass and is closed with behavioral tests or narrowly justified
  unreachable-branch annotations.

## Architecture and documentation

- [x] **ARCH-001 (medium): integration-level parity.** Extend conformance beyond
  codecs and narrow HTTP scenarios to fan-out, fragmentation, quotas, governance,
  and resume ownership.
- [x] **ARCH-002 (medium): capability truthfulness.** Served capability manifests
  and documentation never claim unavailable or partial behavior.
- [x] **ARCH-003 (medium): C# server decomposition disposition.** The post-fix
  review found no bounded extraction that lowers lifecycle coupling without
  splitting the generation/fence invariants across more call boundaries. Retain
  the current partial-class layout and its public-route concurrency tests; revisit
  only when a cohesive behavior-preserving helper has multiple real consumers.
- [x] **ARCH-004 (high): owner-gated step operations.** Browser WebSocket step
  actions in every served backend require current hijack ownership and reject a
  competing operator without worker delivery.

## Verification ledger

| Date | Track | Command | Result |
|---|---|---|---|
| 2026-07-31 | Baseline | `uv run python spec/validate_conformance.py` | 66 entries checked; pass |
| 2026-07-31 | Baseline | `GOWORK=off go test ./...` | pass |
| 2026-07-31 | Baseline | `dotnet build -c Release --no-restore` | pass with existing warnings |
| 2026-07-31 | Baseline | `npm test --workspace=packages/provide-uterm-ts -- --run` | 10,469 pass after native dependency setup |
| 2026-07-31 | Baseline | `uv run pytest -q tests/conformance/ -o addopts=--import-mode=importlib` | 170 pass after frontend asset build |
| 2026-07-31 | Known red | `npm run build --workspace=packages/provide-uterm-ts` | fails: build config drops ES2023 library |
| 2026-07-31 | Known red | `npm run test:ts:coverage` | tests pass; strict coverage threshold fails |
| 2026-07-31 | Known red | `uv run pytest -q packages/provide-uterm-server/tests/` | tests pass; strict coverage threshold fails at `config_schema.py:160` |
| 2026-07-31 | Python robustness | Focused PAM, graphical endpoint, Cloudflare JSON, config-schema, and VNC warning-as-error suites | 137 passed, 1 optional-vendor skip; ruff passed |
| 2026-07-31 | TypeScript readiness | clean emit, native Node 22 root-import smoke, typecheck, Biome, SSH tests, strict coverage | 10,470 tests; 100% statements/branches/functions/lines; pass |
| 2026-07-31 | Browser quality audit (known red) | Node 22 frontend/app build, typecheck, lint, and coverage | builds/typechecks pass; 523 frontend and 330 app tests pass; frontend threshold fails at 64.93/60.34/66.01/67.35%; app has no threshold and reports 54.69/46.62/53.75/54.51%; lint reports 116/8 warnings |
| 2026-07-31 | Browser quality fix-forward (superseded) | Node 22 frontend/app isolated builds, typechecks, warning-fatal lint, and coverage | pass at `c9d265dc`; frontend 552 tests and 94.09/85.79/92.90/95.58% against 90/85/90/90; app 369 tests and 94.18/81.27/96.04/96.98% against 90/80/90/90; independent review was pending at this checkpoint and is closed by the Browser independent closure (`f5e76dbb`) in the next row |
| 2026-07-31 | Browser independent closure | focused owner/VNC tests plus full frontend coverage/typecheck/warning-fatal lint | approved at `f5e76dbb`; 28 focused and 556 full frontend tests pass; 94.22/85.89/92.94/95.73%; app remains 369 pass at 94.18/81.27/96.04/96.98%; four-file fix-forward scope accepted |
| 2026-07-31 | Python server final gate (known red) | `uv run pytest -q tests` in `provide-uterm-server` | 6,726 passed, 2 deselected, 2 approval tests failed; 99.77% coverage with 22 lines and 17 branches missing in newly fenced lifecycle paths; approval store/replay audit reopened before release |
| 2026-07-31 | C# final quality gate (known red) | `make quality-gate` in `provide-uterm-csharp` | build, conformance, and 1,366 serial test-batch cases pass; merged coverage is 95.34% (16,819/17,641) against 97.4%, so binary stage is not reached |
| 2026-07-31 | C# quality-gate closure | `make quality-gate` in `provide-uterm-csharp` | pass in combined commit `5793a7b3`: build clean; 13 conformance and 1,665 serial tests pass; 97.44% (17,189/17,641) >= 97.4%; lowercase `uterm`/`uterm-manager` binaries validated |
| 2026-07-31 | Python approval/lifecycle closure | full strict server suite plus focused approval/lifecycle set, Ruff, and hooks | pass at `99d07111`: 6,752 passed, 2 deselected; 100% of 13,461 statements and 3,592 branches; 77 focused tests pass |
| 2026-07-31 | Python approval expiry/fan-out truthfulness fix-forward (superseded) | normal package-CWD strict suite in one invocation; 268 focused compatibility tests; Ruff/format/hooks | pass at `e32c8a39`: 6,763 passed, 2 deselected; 100% of 13,533 statements and 3,626 branches; independent acceptance was pending at this checkpoint and is closed by the Python approval/resume final independent closure (`4219b154`) below |
| 2026-07-31 | Python rejected-resume authority closure (superseded) | 76 focused server and 20 mirrored core tests; normal strict server suite; Ruff/Mypy/ty/hooks | pass at `ff1955d4`: 6,773 passed, 2 deselected; 100% of 13,558 statements and 3,638 branches; independent final review was pending at this checkpoint and is closed by the Python approval/resume final independent closure (`4219b154`) below |
| 2026-07-31 | Cloudflare lifecycle fix-forward (superseded) | full non-e2e suite; clean-PATH Node 22 pywrangler/workerd adapter; focused fencing/regex; Ruff/Mypy/Bandit | pass in combined commit `5793a7b3`: 1,618 passed, 61 skipped; real adapter passed in 61.23s with hibernation, replacement pause, owned delivery, heartbeat identity, invalid-regex zero-frame, and resume-disabled proof; later independent review reopened cold-runtime, generation, and regex evidence before final closure |
| 2026-07-31 | Cloudflare lifecycle/regex/coverage closure (superseded) | prescribed strict package suite; native Node 22 pywrangler/workerd lifecycle adapter; Ruff/Mypy/Bandit/hooks | pass at `196008a8`: 1,681 passed, 61 skipped; 100% of 4,188 statements and 1,452 branches; omitted-lower counted quantifiers, alternation, and all lookaround forms reject with zero worker frames; later review reopened the one-second resumed-lease expiry race, closed by `c1cea959` in the next row |
| 2026-07-31 | Cloudflare resumed-lease expiry closure | delayed-success focused tests; prescribed strict package suite; native workerd lifecycle adapter; Ruff/Mypy/Bandit/hooks | independently approved at `c1cea959`: 1,682 passed, 61 skipped; 100% of 4,195 statements and 1,454 branches; one-second post-pause expiry compensates and rejects without false advertisement |
| 2026-07-31 | Native capture | `make clean && make test && make all`; UBSan; symbol gate; real macOS injection | 13 writer tests and injection pass; Linux LD_PRELOAD execution added to CI |
| 2026-07-31 | C# lifecycle | Full C# suite plus focused fragmentation, admission, quota, broadcast, resume, ownership, replacement, timeout, and teardown matrices | 1,330 passed, 0 failed; final spec and quality reviews approved |
| 2026-07-31 | Python fan-out | Focused authorization, approval release, route coverage, governance factory, and Ruff gates | 48 tests passed; Ruff passed |
| 2026-07-31 | Go fan-out | `GOWORK=off go test ./...`; `GOWORK=off go vet ./...` | pass |
| 2026-07-31 | C# fan-out | Full serial Release suite plus focused execution/server/config tests | 1,338 passed, 0 failed; Release build passed |
| 2026-07-31 | TypeScript fan-out | Full TypeScript suite, typecheck, and Biome lint | 10,474 tests passed; typecheck and lint passed |
| 2026-07-31 | Live fan-out strict admission | Python/Go/C# servers × Python/Go/C#/TypeScript clients | 12 of 12 cells passed; TypeScript server explicitly unadvertised |
| 2026-07-31 | Historical fan-out coverage manifest (superseded) | `uv run python scripts/validate_fanout_security_coverage.py`; focused validator tests | Historical checkpoint only: the declaration/regex approach passed 7 tests, then independent review rejected it; it was replaced by the executable semantic fixture and runner |
| 2026-07-31 | Fan-out independent review (superseded) | Security, controller-invariant, fast-output, store-concurrency, timeout, and semantic-conformance review | rejected; FANOUT-001 through FANOUT-005 reopened pending corrected implementation and re-review; corrected and re-reviewed in the Fan-out final independent acceptance (`ebc6b50f`) below |
| 2026-07-31 | Live capability intersection | `uv run pytest -q tests/conformance/live/test_matrix.py` | 18 passed; required client/server capabilities enforced; TypeScript fan-out server explicit unsupported/unserved |
| 2026-07-31 | Combined fan-out conformance checkpoint | `uv run pytest -q tests/conformance/live/test_matrix.py tests/conformance/test_fanout_security_coverage.py` | 22 passed, 1 failed in sibling-owned semantic runner; Go inherited invalid `go.work` and C# adapter had not yet produced observations |
| 2026-07-31 | Fan-out documentation truth (superseded) | ARD and security/protocol matrices reviewed against served implementations | all five operations global-admin-only; served, unserved, and explicitly unsupported policy surfaces distinguished; FANOUT-001 through FANOUT-005 remained in review at this checkpoint and are closed by the Fan-out final independent acceptance (`ebc6b50f`) below |
| 2026-07-31 | Go lifecycle fence (superseded) | `python3 scripts/run_session_lifecycle_security_scenarios.py --backend go`; `GOWORK=off go test ./... -count=1`; `GOWORK=off go test -race ./hub ./server -count=1`; `GOWORK=off go vet ./...` | tests passed at `f71d34e2`, but independent concurrency review rejected the checkpoint; eight lifecycle, delivery-truthfulness, and approval ABA findings remained open pending a fix-forward commit and re-review; closed and accepted at `5ef24f82` below |
| 2026-07-31 | Go lifecycle fix-forward (superseded) | lifecycle adapter; `GOWORK=off go test ./... -count=1`; focused and full `GOWORK=off go test -race ./...`; `GOWORK=off go vet ./...` | pass at `25d86f2a`; deterministic release/acquire, replacement/disconnect, resume, approval ABA/partial replay/order, tunnel-step, and writer-cancellation regressions green; independent follow-up review still remained at this checkpoint and is closed by the accepted `5ef24f82` row below |
| 2026-07-31 | Go final lifecycle/governance closure | focused reopened-area tests and race tests; full hub/server race; module tests and vet | independently approved at `5ef24f82`; all seven REST compensation, tunnel refusal, approval ABA, atomic hold, mode-transition, token-continuity, and reconnect/detach findings closed; reconciliation note (2026-08-01): the fence rejection counted eight findings — these seven plus the delivery-truthfulness area, whose partial replay/order and writer-cancellation regressions are recorded in the `25d86f2a` row above |
| 2026-07-31 | Native C capture | `make clean && make test` in `native/capture` | 13 self-tests and exported-symbol gate pass |
| 2026-07-31 | Native C PAM | `make clean && make test` in `native/pam_uterm` | PAM JSON-escape/static-helper self-test passes |
| 2026-07-31 | Python annotation package | `uv run pytest -q tests` in `provide-uterm-annotation` | 81 passed; 100% statements and branches |
| 2026-07-31 | Python client package | `GOWORK=off uv run pytest -q tests` in `provide-uterm-client` | 1,277 passed, including live Go interop; 100% statements and branches |
| 2026-07-31 | Python platform package | `uv run pytest -q tests` in `provide-uterm-platform` | 1,387 passed, 18 environment/platform skips; 100% statements and branches; native self-tests recorded separately |
| 2026-07-31 | Python core regression diagnosis | max-group-size and WebSocket failure focused public-route suites with explicit timeout | initial strict run exposed an admission-masked assertion, then an indefinite receive from a stale delivery mock; both corrected and 22 focused tests pass |
| 2026-07-31 | Python core final gate | `uv run pytest -q tests --timeout=90 --timeout-method=thread` in `provide-uterm` | 3,745 passed, 17 documented skips, 126 deselected; 100% of 6,248 statements and 1,662 branches |
| 2026-07-31 | Python approval/resume final independent closure | 119 focused implementation tests; normal strict server suite; independent 113-test approval/resume review | independently approved at `4219b154` over `e32c8a39` and `ff1955d4`: 6,774 passed, 2 deselected; 100% of 13,564 statements and 3,640 branches; approval expiry/delivery, rejected-resume authority, and delayed rev1/reused-rev2 fan-out state are closed |
| 2026-07-31 | Fan-out semantic matrix (superseded) | `uv run python scripts/run_fanout_security_scenarios.py` | pass for Python, Go, C#, and TypeScript components; unknown members reject by default and configured permissive admission remains explicit; run predates the `d092f5d0` runner bound and is superseded by the central-runner re-run in the Fan-out final independent acceptance (`ebc6b50f`) below |
| 2026-07-31 | Bounded fan-out runner closure (superseded) | focused timeout tests; full fan-out conformance file; central semantic runner; Ruff/hooks | pass at `d092f5d0`: overridable 120-second default adapter bound fails closed with zero partial observations (exercised at an injected 1-second bound; the 120-second default itself is pinned by the 2026-08-01 conformance row below); 600-second outer test bound; 22 conformance tests pass; accepted by the Fan-out final independent acceptance (`ebc6b50f`) below |
| 2026-07-31 | TypeScript approval ABA closure | focused approval/fan-out tests; full strict package suite; typecheck/lint/build/smoke; central runner | independently approved at `ebc6b50f`: 10,513 tests and 100% all coverage metrics; exact-revision claim/resolve/expiry/release, duplicate/reuse, and safe counter exhaustion pass |
| 2026-07-31 | Fan-out final independent acceptance | cross-language implementation audit; central semantic runner; full fan-out conformance; focused native tests | approved at `ebc6b50f` over `d092f5d0`: central Python/Go/C#/TypeScript matrix passes; 22 conformance and 107 focused TypeScript tests pass; all reopened fan-out findings closed |
| 2026-07-31 | Final lifecycle semantic matrix | `uv run python scripts/run_session_lifecycle_security_scenarios.py` | pass for Python, Go, C#, and Cloudflare native served/unsupported cells |
| 2026-07-31 | Conformance suite (superseded) | `GOWORK=off uv run pytest -q tests/conformance/ -o addopts=--import-mode=importlib` | 213 passed in 68.09s; run predates the `d092f5d0` adapter-timeout test and is superseded by the 2026-08-01 re-run below |
| 2026-07-31 | Docs/spec/workflow validation (superseded) | docs accuracy; static spec symbol validation; safe-load all workflow YAML | pass; 66 spec entries (58 required) checked and 8 workflow files parsed; validated content predates the `963e5692` ledger edits and is superseded by the 2026-08-01 re-validation below |
| 2026-08-01 | Conformance suite (superseded) | `GOWORK=off uv run pytest -q tests/conformance/ -o addopts=--import-mode=importlib` | 215 passed in 68.13s, including the `d092f5d0` adapter-timeout test and a new test pinning the 120-second default adapter bound; run predates the fan-out admission scenarios and is superseded by the re-run below |
| 2026-08-01 | Fan-out member admission (single-authority fix) | shared contract gained six new create-path scenarios; Python reference rewritten; Go/C#/TypeScript aligned; `uv run python scripts/run_fanout_security_scenarios.py` | pass for Python, Go, C#, and TypeScript. Python and TypeScript had classified members through the controller's own resolver and consulted the registry only for members the controller had ALREADY refused, so a member the controller wrongly approved was never checked against the registry's definition at all — A/B verified pre-fix `200` where the contract now requires `403`. Admission is now decided from ONE registry resolution per member, in positional order, with read access checked against that same definition. Go and C# already had this shape and needed no route change. |
| 2026-08-01 | Fan-out authorization wiring gate | new `authorization_ready` on the controller, gating create in all four backends; per-backend A/B with the gate neutered | pass. Python's `create` fail-closed behavior on an unwired controller was untested in the shared contract and absent from Go, C#, and TypeScript; a half-wired controller would have created groups on whatever checks remained. Pinned by `create_refuses_when_controller_authorization_unwired` (403 `authorization_unavailable`), which every backend was A/B'd against: neutering the gate flips exactly that scenario to `200` and leaves the other 19 unchanged. |
| 2026-08-01 | Final conformance suite | `GOWORK=off uv run pytest -q tests/conformance/ -o addopts=--import-mode=importlib` | 219 passed in 78.22s, including the four new contract-shape category tests and the real four-backend runner |
| 2026-08-01 | Final docs/spec/workflow validation | ledger supersession repair; `uv run python spec/validate_conformance.py`; safe-load all workflow YAML | pass; 66 spec entries (58 required) checked across 5 categories; 8 workflow files parsed; supersession labels unified to `(superseded)` with named closing-row pointers and previously deleted checkpoint evidence restored |

