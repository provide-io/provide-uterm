# uterm Code Review Remediation Tracker

This tracker records the actionable findings from the 2026-07-31 comprehensive
architecture and implementation review. Status is evidence-based:

- `[ ]` pending
- `[~]` implementation or review in progress
- `[x]` focused tests and the relevant quality gate pass

Design: `docs/superpowers/specs/2026-07-31-code-review-remediation-design.md`

## Cross-language fan-out security

- [ ] **FANOUT-001 (high): configurable unknown-member admission.** Python, Go,
  C#, and TypeScript reject unknown worker IDs by default; one consistently named
  option permits dormant IDs. Acceptance: strict/default and permissive tests exist
  in each served implementation.
- [ ] **FANOUT-002 (high): send-time session authorization.** Every group member is
  resolved and authorized on every send and approval release. Unknown or revoked
  members receive a failure and no input. Group grants do not bypass session authz.
- [ ] **FANOUT-003 (high): governance policy enforcement.** Python factory wiring
  supplies a real fan-out policy adapter and fails closed when configured policy is
  unavailable. Go and C# cannot silently bypass configured governance.
- [ ] **FANOUT-004 (medium): C# semantic parity.** Parallel/sequential execution,
  response collection, elapsed time, stop-on-first-error, and divergence behave as
  advertised or the surface explicitly reports unsupported behavior.
- [ ] **FANOUT-005 (high): shared parity coverage.** Cross-language behavioral
  scenarios cover unknown IDs, revoked authorization, group grants, policy deny,
  policy hold/release, and partial member failures.

## C# WebSocket and connection lifecycle

- [ ] **CSHARP-WS-001 (high): fragmented browser messages.** Browser control and
  input messages are accumulated through `EndOfMessage` with a bounded size.
- [ ] **CSHARP-WS-002 (high): fragmented worker messages.** Worker snapshots and
  control frames retain decoder state across receive fragments.
- [ ] **CSHARP-WS-003 (high): fragmented tunnel messages.** Tunnel frames are
  decoded only after a complete bounded WebSocket message is assembled.
- [ ] **CSHARP-CONN-001 (medium): worker admission.** Both worker and tunnel routes
  honor failed `RegisterWorker` results and do not mark rejected workers online.
- [ ] **CSHARP-CONN-002 (medium): browser quotas.** Enforce
  `MaxConnectionsPerPrincipal`, including rollback on setup and disconnect.
- [ ] **CSHARP-CONN-003 (medium): handshake ordering.** Deferred browser activation
  prevents normal broadcasts before `hello` and initial state frames.
- [ ] **CSHARP-CONN-004 (medium): bounded broadcast.** Browser sends are concurrent
  or isolated, have a timeout, and prune failed sockets.
- [ ] **CSHARP-RESUME-001 (medium): bounded token storage.** Expired or abandoned
  resume tokens are swept and total storage is capped.
- [ ] **CSHARP-RESUME-002 (medium): truthful resume semantics.** Successful resume
  restores documented ownership/state; otherwise the capability is not advertised.
- [ ] **CSHARP-AUTH-001 (low): constant-time worker credential comparison.** Worker
  and tunnel bearer validation use a constant-time byte comparison.

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

## Python robustness and quality

- [x] **PY-PAM-001 (low): non-object JSON.** PAM listener rejects arrays, strings,
  numbers, booleans, and null without terminating the connection handler.
- [x] **PY-GRAPH-001 (low): endpoint parser errors.** Malformed bracketed IPv6 and
  related URL parser errors become `GraphicalTargetError` with a client-error shape.
- [x] **PY-CF-001 (low): invalid JSON body.** Cloudflare request decoding handles
  `JSONDecodeError` consistently with other invalid request bodies.
- [x] **PY-TEST-001 (medium): thread cleanup.** The VNC relay regression test emits
  no `PytestUnhandledThreadExceptionWarning`.
- [~] **PY-COV-001 (medium): restore server coverage gate.** The normalized-empty
  Cloudflare team-domain path is tested and server coverage returns to 100%.

## Architecture and documentation

- [ ] **ARCH-001 (medium): integration-level parity.** Extend conformance beyond
  codecs and narrow HTTP scenarios to fan-out, fragmentation, quotas, governance,
  and resume ownership.
- [ ] **ARCH-002 (medium): capability truthfulness.** Served capability manifests
  and documentation never claim unavailable or partial behavior.
- [ ] **ARCH-003 (medium): C# server decomposition follow-up.** After behavioral
  fixes, extract shared WebSocket receive/admission/resume helpers where this lowers
  lifecycle coupling without an unrelated rewrite.

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
| 2026-07-31 | Native capture | `make clean && make test && make all`; UBSan; symbol gate; real macOS injection | 13 writer tests and injection pass; Linux LD_PRELOAD execution added to CI |
