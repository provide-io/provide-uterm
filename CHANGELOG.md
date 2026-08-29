# Changelog

All notable changes to provide-uterm are documented in this file.

## [Unreleased]

## [0.5.2] — 2026-08-29

A flake cycle: three tests that had been failing
intermittently in CI were each root-caused rather than retried, and none of the
three turned out to be timing noise. One was a defect in shipped code. A nightly
profile was also found to have been failing since a refactor eight days earlier,
with nothing anywhere to say so.

### Fixed

- **The human VNC relay reset connections instead of closing them.** Both pumps
  cancelled a shared token when either direction ended, and the pump still
  running is parked on a read — on the client side, a `ws.ReceiveAsync` holding
  that same token. Cancelling it *aborts* the WebSocket rather than closing it,
  which resets the connection: the peer is denied its close handshake and
  anything the transport had not flushed is discarded. The graceful `CloseAsync`
  further down could never run, because by the time it looked the state was
  `Aborted` and its guard skipped it. The relay now announces the close while the
  socket is still alive and cancels only if the peer does not answer inside the
  existing two-second drain. A real VNC client was being reset rather than closed
  at end of session, and could lose the tail of the stream.

### Testing and CI

- **The telnet snapshot wait was a ten-second budget that only ever waited half a
  second.** `drain_for_snapshot_with_text` polls in 0.5s slices so it can
  re-check its own deadline, but it treated a slice that ended without a snapshot
  as end-of-stream and returned. It passed whenever the first slice happened to
  catch a frame and failed when the runner was starved.
- **The C# sequential-budget test decided on arithmetic rather than on the
  deadline.** The second member's collect is clamped to exactly what the first
  left, so the two nested bounds always summed to within one timer tick of the
  budget, and a tick-rounded timer landing early left `RemainingMs` at 1 — enough
  to send a member that should have been skipped. The hub's read now ignores its
  per-read bound, leaving the shared deadline as the only thing that can end it.
  Measured on Windows: 6 failures in 135 before, 0 in 135 after.
- A dispatch-only flake hunt (`ci/csharp_flake_hunt.sh`) repeats one test many
  times per arm on a Windows runner and reports a rate, because a ~4% flake is
  invisible to a single run in either direction. Its first form looped the test
  alone and uninstrumented and reported 0 failures in 400 for *both* arms —
  it reproduced nothing, so it measured nothing. Repeating what actually failed
  — `make cover-batch BATCH=2`, coverlet-instrumented and single-threaded —
  reproduced it at 4.4%, matching the observed CI rate. The conditions were the
  whole story.
- **The memray webhook profile had not run since 2026-08-18.** It stubbed the
  network by assigning to `webhooks.httpx2.AsyncClient`; `b07da1ca` routed
  outbound HTTP through one client factory and webhooks stopped importing httpx2
  at all, so the assignment raised `AttributeError` on every nightly run.
- **A red scheduled CI run is now reported on your next push.**
  `report_red_workflows.sh` excluded CI on the grounds that a push already fails
  on it — true of the jobs a push runs, and false of the ones it does not. The
  scheduled run carries jobs no push executes, so those had no reader at all,
  which is how the memray break sat unnoticed for six days. Failing jobs a push
  would never have run are marked as such. Advisory, and silent when green.
- **External state is now read in the same turn it is reported.** A state claim
  is only true as of the moment it was read, and three wrong answers in one
  session came from the gap: a release run called "never triggered" that fired
  fifteen minutes later, browsers called logged-out four calls after logging in,
  and a scheduled run called green because its one failing job sat at position 31
  of 49 and the jobs API stops at 30. `scripts/state.sh` makes re-reading one
  timestamped call — git position, push *and* scheduled CI with failing jobs, the
  release run, and both package indexes — and encodes those three traps directly.

### Release

- The PyPI publish steps carry `skip-existing: true`, which the TestPyPI steps
  always had. Without it a release re-run reaches PyPI, gets "file already
  exists" and fails the job — and `sign-and-release` needs all three PyPI jobs,
  so the Sigstore bundles, release assets and SLSA provenance became unreachable
  for any version whose files were already up. Nothing is hidden: PyPI refuses to
  replace an existing file either way, so the only change is fatal to no-op.
- **Release verification installed extras-gated packages without their extras.**
  The 0.5.1 run died at `Verify TestPyPI · provide-uterm-platform` with
  `ModuleNotFoundError: No module named 'fastapi'`: `provide.uterm.manager`
  imports fastapi eagerly and fastapi lives behind the `[manager]` extra, so a
  bare install cannot import it. `PackageSpec` now carries `install_extras` and
  the verify step reads the install spec from `scripts/package_metadata.py`
  rather than keeping a second copy of the table in shell. Only platform needs
  one — server and client import cleanly bare, and requesting extras a package
  does not need would have verification exercise a fatter environment than any
  user gets, hiding the next packaging regression instead of catching it.

## [0.5.1] — 2026-08-25

A cross-language hardening cycle. The C# port gained the capability work it was
missing, and a run of gaps turned up where suites existed but nothing invoked
them — several defects were sitting behind gates that had never executed.

### Added

- C# port: the browser-input approval hold path, the tenant-scoped
  graphical-target registry, and snapshot ingest counters.
- Wire-contract drift is now gated across the ports in CI, and the shared
  graphical-targets corpus is executed rather than merely shipped.

### Fixed

- **Approval requests never expired in the C# port.** Nothing was aging them
  out, so a hold could outlive its window.
- C# builds: eight warnings cleared and ratcheted against regression; the
  warning gate now sees analyzer warnings and not only compiler ones; native
  AOT is kept off framework-dependent publishes.
- The capture layer no longer folds a stale-library remedy into the redacted
  value.
- The control decoder is seeded through the buffer it actually drains. (The
  reset in `finish()` that this cycle removed as "unobservable" was put back on
  merge: the error hook runs between it and the `except` clause that repeats
  it, so a caller can see the difference, and a hook that raises escapes the
  handler entirely and would leave the rejected frame's bytes in place.)
- Release-version consistency no longer sweeps a git submodule's `VERSION`
  into this repository's invariant.
- `localStorage` is provided to the app test suite when the runtime's own
  global shadows the one jsdom installs (node >= 24).
- **`connect_async_ws` could block for the whole close timeout.** websockets
  stops reading the transport once its receive queue fills, so a caller that
  read the frame it wanted and then left the context manager stopped consuming
  with frames still queued. The peer's close frame then sat unread and `close()`
  waited on a reply its own paused reader would never collect — ten seconds by
  default. The socket is now drained while it closes: 125 stalls in 600 sessions
  before, none after.

### Testing and CI

- `tests/conformance/` and `tests/scripts/` are wired into CI and the local
  sweep. Both sat outside the default testpaths and were named by no gate, so
  the cross-language API-parity spec and the tooling suite had never run.
- `run_all_tests.py` runs every leg and reports a per-leg summary instead of
  returning on the first failure, which had been hiding every later suite.
- The mutation gate fails on a perimeter file that generates no mutants,
  nineteen allowlist entries for mutants that are in fact killed were dropped,
  and the renumbered `sessions` entries were repaired.
- A fan-out adapter that blows its deadline now reports where it hung.
- The hostile probe no longer charges connection teardown to server latency, and
  its `stop` kills whatever holds the port rather than assuming a pidfile.
- The goldens check no longer fails on a broken pipe it caused itself.

## [0.5.0] — 2026-08-23

A multi-wave security/compliance hardening pass landed during the
`0.5.0` development cycle. Highlights:

### Cross-port hardening (2026-08-01 … 2026-08-23)

- **WebSocket authentication is refused consistently across all three server
  ports.** An unauthenticated socket is answered with 401 rather than 403; Go
  refuses before the upgrade rather than after it; C# refuses anonymous browser
  sockets. The worker socket — the privileged half, which had no security
  probe of its own — is now covered, and `UTERM_TEST_MODE` announces itself
  when it disables websocket auth instead of doing so silently.
- **A browser that is still starting up no longer misses inspect traffic.**
  All three ports hold inspect frames for a connecting browser and replay them
  over a broadcast window, so the frames emitted between socket accept and
  first render are delivered rather than dropped.
- **Approval requests expire (C#).** Nothing was expiring them, so a request
  that was never answered held its slot indefinitely. The browser-input
  approval hold path is ported to match the reference.
- **The control-frame length header has one canonical form.** Every decoder
  validated the field only for membership in the hex alphabet while every
  `is_control_frame` predicate compared it against the canonical lower-case
  spelling, so an uppercase header was a frame to one and terminal data to the
  other — and the predicate is what decides whether a payload is framed at all,
  so a conforming peer emitting `%08X` would have had its control frames
  rendered to the screen as text. Fixed in Python, Go, C# and TypeScript
  together, pinned in the shared fuzz corpus as CCF-REG-0006. Found by the
  weekly exploratory fuzz job, which had been reporting it on fresh seeds for
  a month.
- **The control-frame decoder cannot spin.** The drain loop is bounded, so a
  parse offset that fails to advance rejects the stream instead of looping
  forever inside the caller's read loop. The plain-data scan now jumps to the
  next DLE with a C-level search rather than stepping a byte at a time —
  4096 bytes of terminal output decode in 1.7µs, down from 169µs.
- **Capture reports what it could not deliver**, including when the built
  library is behind its sources, rather than reporting a clean run.
- **Snapshot freshness arithmetic is pinned**, and `/snapshot` no longer
  answers from a cache without saying so.
- **Mutation tooling.** Narrowed runs read their results before the config is
  restored (a 976-mutant run with 456 survivors had been read back as
  `total=0` and passed as clean); a perimeter file that generates no mutants
  fails instead of silently enforcing nothing; a perimeter-wide question is
  dispatched to the chunked workflow instead of timing out; a kill proven by
  one attempt survives a later attempt's timeout; and the Go, C# and
  TypeScript allowlists key on mutation content rather than line coordinates,
  so an edit above an entry no longer invalidates it.
- **Dependencies.** provide-telemetry 0.8.0 on the Python and C# sides with the
  submodule dropped in favour of published packages, a Go toolchain bump
  closing six stdlib advisories, and CF-image pins for undici, brace-expansion,
  ip-address and node-tar (CVE-2026-73566).

### Post-audit hardening (2026-06-02 … 2026-06-06)

A full-codebase internal re-audit plus two external code reviews drove a focused
remediation pass; every actionable finding is closed.

- **Cloudflare DO credential-stripping (critical).** `update_kv_session` is now
  read-modify-write, so the 60 s status heartbeat no longer null-outs the worker/
  share/control token hashes — tunnel/share/control auth survives every worker
  connect (`cloudflare/state/registry.py`).
- **Native capture builds on Linux (critical).** `capture_disable()` moved to the
  shared section so the `-Werror` Linux build compiles (`native/capture/capture.c`).
- **Telnet IAC subnegotiation-overflow leak.** An oversized telnet subnegotiation
  (past the 4 KB cap) no longer drops out of SB-parsing mode mid-stream, so its
  tail can't leak into the cleaned application data (`gateway/_iac_negotiate.py`).
- **DO webhook delivery offloaded off the broadcast critical path.** A slow or
  blackholed webhook URL can no longer stall a Durable Object's PTY stream:
  delivery runs as a bounded, `asyncio.wait_for`-timed background task
  (`cloudflare/do/_webhooks.py`, `do/session_runtime/io.py`).
- **CaptureConnector stdin forwarding is non-blocking.** `_forward_stdin` uses an
  asyncio Unix stream instead of a blocking `socket.sendall`
  (`pty/capture_connector.py`).
- **Lows L1–L7** all fixed: gateway token-file TOCTOU (atomic 0600 create), CF
  webhook-pattern ReDoS cap, hijack approval-timer leak on dispose, PTY
  `handle_input` `os.write` OSError guard, annotation cross-boundary carry, and
  `pty/connector.py` mutation-pragma hygiene (killable codec/buffer mutants now
  killed; genuine equivalents documented in `mutation_equivalents.toml`).

### Security

- **Tamper-evident WORM audit chain.** `server/audit_chain.py` writes a
  sha256 hash-chain (genesis-anchored, durable head + anti-rollback) over
  audit events, verifiable post-fact via the `uterm audit verify <path>`
  CLI subcommand (`cli/audit.py`). The optional HMAC/ed25519 signing tier
  remains a deferred enterprise spec; the shipped chain is sha256-only.
- **Connector-egress SSRF guard.** Session creation/update now flow through
  a `SessionRegistry` chokepoint (`assert_session_egress_allowed`,
  `server/egress.py`) that can block private/loopback/cloud-metadata
  targets (including IPv4-mapped/NAT64/6to4 IPv6 forms), gated on
  `security.block_private_connector_targets` (default `False`). The same
  guard covers the PAM relay and webhook destinations.
- **Webhook-IdP response integrity.** `WebhookIdentityProvider` verifies the
  IdP response HMAC signature by default
  (`auth.webhook_idp_require_signed_response`, default `True`; fails closed
  at config-load if no secret is configured) and adds replay protection
  (bounded replay cache + optional response-nonce binding via
  `auth.webhook_idp_require_response_nonce`).
- **Per-agent manager worker tokens.** The External Management Tier can bind
  worker tokens to their `agent_id` (`enforce_per_agent_worker_token`,
  default `False`) and constrains self-reported register fields to block
  worker-token privilege escalation on `/register`.
- **Default recording redaction.** A built-in secret-redaction ruleset
  (AWS keys, GitHub/Slack tokens, JWTs, etc.) ships in
  `bridge/hub/redaction_defaults.py` and is enabled by default via
  `recording.redact_sensitive=True`; operators opt out explicitly.
- **Transport / file-permission hardening.** Recording files are created
  0o600 with TOCTOU-safe open flags; telnet/SSH/WS connectors validate the
  post-connect peer IP (DNS-rebind mitigation); `WebSocketTransport` is
  hardened for the `websockets` 16.x API. The PAM-notify and capture
  unix-domain sockets now set a restrictive umask **before** bind, so they
  are created 0o600 with no permission window (was bind-then-`chmod`).
- **Code-review remediation (2026-06-02 bridge/transport/platform/MCP review).**
  - **Hijack acquire no longer holds the global hub lock across worker I/O.**
    `try_acquire_rest` reserved the slot under `TermHub._lock` and sent the
    worker-pause frame *while holding it*, so one backpressured worker could
    stall every hub operation (an availability hazard). It now reserves
    (`hijack_pending`), sends the pause lock-free, then finalises under the
    lock (`bridge/hub/lease.py`).
  - **MCP regex ReDoS guard.** User/LLM-supplied patterns for
    `session_watch`/`session_subscribe` are screened for catastrophic-
    backtracking constructs before `re.compile`, and `session_watch` now
    clamps `max_events` like `session_subscribe` (`ai/patterns.py`,
    `ai/server_impl.py`).
  - **Atomic capture framing.** The native interposer (`capture.c`) emits
    each frame's header+payload in a single syscall, closing a
    multi-threaded framing-corruption race on the shared capture fd.
  - **Annotation fallback no longer leaks matches.** A rule whose
    `description_template` fails to format falls back to a label-only
    description, never embedding the raw regex match (defense-in-depth
    against a misconfigured rule leaking a matched secret to telemetry).

### Reliability / performance

- **Concurrent browser broadcast.** `MessageRouter.broadcast` fans out the
  per-browser sends with `asyncio.gather` (roles pre-resolved to avoid a
  redaction-cache race) instead of awaiting each in series, so one slow
  viewer no longer delays the rest (`bridge/hub/router_impl.py`).

### Testing / CI

- **Resource-exhaustion (hostile-client) workflow** —
  `.github/workflows/hostile-client.yml` runs burst, oversized-frame, and
  slow-loris probes.
- **`/readyz` readiness gate** added alongside the existing health/liveness
  probes for orchestrator integration.
- **Hostile-client probe asserts _survival_, not connection-success.** The
  burst / oversized-frame / slow-loris probes (`scripts/hostile_profile.py`)
  now classify each attempt (refused / completed / hung / error) and pass
  when the server stays healthy and refuses or bounds every attempt — the
  correct signal against the fail-closed auth posture (an unauthenticated WS
  connect refused at the 403 boundary is healthy, not a failure). The
  auth-gated probes flag a _completed_ unauthenticated handshake as an auth
  bypass (`--require-refused`).
- **Deterministic CI.** De-flaked the worker-disconnect frame-ordering tests
  (two racing broadcast tasks → assert on frame membership, not arrival
  order, in both the core and server test copies) and replaced the PTY
  throughput test's hardcoded fps target with a regression floor. Several
  `connection.py` and `lease.py` `async with` exit-arcs carry
  `# pragma: no branch` — a coverage.py-on-Python-3.11 arc-attribution
  quirk, not an untested branch.
- **Mutation perimeter — lease.py + models.py at killed==100.** The
  two-phase hijack reserve is pinned by a dedicated kill suite (lock-free
  during send; reservation/rollback on failure, cancellation, vanished or
  superseded worker). Editing `models.py` surfaced `_safe_int`/`_safe_float`
  as latently uncovered in the perimeter ("enumerated ≠ enforced"); a
  focused kill suite now covers them. Both files: 521/521 mutants killed.

### Build / dependencies

- **All Python + JS/TS dependencies upgraded to latest** and re-locked
  (`uv.lock` / `package-lock.json`): includes `provide-telemetry` 0.4.7,
  Starlette 1.2.x, Pydantic 2.13.x, `websockets` 16.x, Ruff 0.15.x, Vite 8.x.
- **Single-source Node toolchain.** `.nvmrc` pins the Node major; CI and both
  Dockerfiles read it (`node-version-file` / `ARG NODE_IMAGE`) so they cannot
  drift; root `package.json` declares `engines`.
- **Multi-stage container images.** `docker/Dockerfile.server` and
  `docker/Dockerfile.cf` build the browser UI in-image (frontend assets are
  no longer committed) and strip build tooling from the runtime images so the
  Trivy HIGH/CRITICAL gate passes on both. `container-scan.yml` now scans the
  Cloudflare image as well, and Dependabot covers the npm + docker ecosystems
  alongside pip + github-actions.
- **DRY transport sessions.** `telnet_session.py` and `ws_session.py` share a
  `TransportSession` base (`provide/uterm/transport_session.py`);
  `TelnetTransport` now explicitly implements the `ConnectionTransport` ABC.

### Breaking Changes

- **WebSocket origin default-deny.** `server.allowed_origins=[]` (the default) now
  rejects cross-origin browser WS upgrades with code 4403. Same-origin browsers
  are auto-permitted; non-browser clients (no `Origin` header) pass through as
  before. Operators relying on the previously-permissive empty default must
  set `allowed_origins=["*"]` explicitly. (See review item #1, fix `5c819fe`
  + same-origin softening `91af35f`.)
- **Tunnel-token storage schema.** `app.state.uterm_tunnel_tokens[*]` and CF KV
  `session:<tid>` entries now hold `worker_token_hash` / `share_token_hash` /
  `control_token_hash` (BLAKE2b digests) instead of raw bearer tokens. Plain
  tokens leave the create/rotate API exactly once in the JSON response and
  never return to in-process or KV state. Existing KV entries written under
  the old `*_token` schema will not authenticate after upgrade — rekey by
  POSTing `/api/tunnels/{id}/tokens/rotate`.
- **Bridge protocol-version negotiation.** Worker `worker_hello` and server
  `hello` frames now carry a `protocol: {min, max, preferred}` block;
  mismatched ranges close the WS with code 1002 + a structured error frame.
  Legacy `protocol_version: int` is accepted as `{min=v, max=v}`. Workers
  that send no protocol field at all default to `{min=1, max=1}`.

### Security

- **HTTP-intercept header denylist.** Operator-supplied headers in the
  `http_action` modify decision are filtered against a denylist (Authorization,
  Cookie, Host, Content-Length, Transfer-Encoding, X-Forwarded-*, Forwarded,
  RFC 7230 hop-by-hop). Closes auth-hijack / IP-spoof / HTTP-smuggling vectors.
- **JWT secret minimum entropy.** `worker_bearer_token` and HS\* JWT secrets
  shorter than 32 characters are rejected in production-like configs
  (`require_jwt_in_production=true` or non-loopback bind). PEM-encoded
  asymmetric public keys are exempt.
- **Control-frame JSON depth bound.** `ControlChannelDecoder` rejects payloads
  nested deeper than `max_frame_depth` (default 32) to prevent stack/CPU DoS
  from deeply-nested `[[[…]]]` payloads under the 1 MB size cap.
- **Non-input control-frame rate-limit.** Browser WS handlers now have a
  separate `browser_control_rate_limit_per_sec` (default 10/s) budget for
  hijack_request / presence_update / resume / queued_input / control_request
  frames so a hostile browser can't flood the hub even when input rate
  limits are sized for legitimate typing.

### Architecture

- **Pluggable Authorization Service:** Refactored `AuthorizationService` into an async, protocol-based gateway. Supports `LocalAuthorizationProvider` (AGPL default) and `WebhookAuthorizationProvider` (managed integration).
- **Extensible Policy Gates:** Introduced `PolicyGate` (Terminal Hub) and `AgentSpawnPolicyGate` (Agent Manager) to intercept real-time input and process spawning.
- **Standardized Telemetry (DAS):** Instrumented core components with the `provide-telemetry` Domain-Action-Status (DAS) schema for high-fidelity structured logging.
- **Node Discovery:** Implemented background heartbeat for automated Node registration with external External Management Tiers.
- **Legacy Cleanup:** Purged 3,000+ lines of legacy vanilla JavaScript frontend; Node now strictly serves the React/Vite dashboard. Simplified API key auth by removing role-marker scope shorthand.
- **`HijackLease` value object.** Wraps the three hijack-ownership fields on
  `WorkerTermState` with an explicit state-machine (idle / dashboard-active
  / REST-active / expired) and `expire(now)` semantics. Backwards-compatible:
  the direct fields still work, but new code should prefer `state.lease`.
- **Durability warning escalation.** Process-local control-plane state on a
  multi-replica orchestrator (Kubernetes, Cloud Run, Azure App Service, AWS
  ECS, Fly.io detected via env vars) now emits an ERROR-level startup log,
  not just an INFO note. Single-replica deployments unchanged.

### Detection

- **`PromptDetector(strict=True)`** raises `DetectorPatternCompileError` on
  any pattern-compile failure instead of silently degrading detection.
  Default `strict=False` keeps the old behaviour. A new `compile_failures`
  property exposes the skipped-pattern list for operator introspection.

### Frontend

- **`presence-bar`, `terminal.ts` a11y**: every toggle button has an
  `aria-label`; settings panel got `role="dialog"`; the status dot's
  `aria-label` updates with connection state; non-color status text added.
- **API + WS boundary validation.** `packages/provide-uterm-app/src/api/
  validators.ts` replaces several `as any` / `as unknown as T` casts in
  `sessions.ts` and `useInspectWs.ts` with runtime shape checks.
- **`hijack.ts` decomposition.** Pure HTML/approval helpers extracted into
  `hijack-ui.ts` and `hijack-approval.ts`; the container class stays.

### Demos

- **Tunnel demo rewritten** to exercise the actual share/control URL flow
  instead of requiring `wrangler dev`. Records two simultaneous browsers
  (control_url for operator role, share_url for viewer role) against an
  in-process echo worker streaming ANSI through `/tunnel/{id}`.
- **`demo_grid` added to the highlight reel** (15 clips total).
- **`reel.py` cache fix** — `_PRIMARY_KEY` default was `"mp4"` which
  produced a literal `mp4_trim.mp4` lookup that never matched any real
  file. Single-browser demos now actually hit the cache on subsequent
  rebuilds (15-second rebuilds vs ~13-minute re-recordings).
- **Manifest builder honours `PRIMARY_VIDEO`** per recorder, so
  multi-browser demos (hijack/deckmux/fleet/fanout) point to their
  actual mp4 (`operator_trim.mp4`, `composite_trim.mp4`, `grid_trim.mp4`)
  instead of silently downgrading to cast-only.

### Tooling / CI

- **mypy + ty in CI.** Promoted from pre-commit `manual` stage to a CI
  quality job (mypy strict on core; mypy + ty informational on the rest
  pending the known cross-file resolution gap in `ty 0.0.19`).
- **CI inline-script extraction.** `ci/prepare_mutation_args.sh` +
  `ci/typecheck.sh` replace multi-line `run:` blocks in `ci.yml` per the
  3-line policy.
- **Hypothesis property tests** for the control-channel codec and
  detection engine.
- **`bridge/contracts.py` added to mutmut `paths_to_mutate`** — the
  `negotiate_protocol_version` boundary is now mutation-covered on full
  gate runs. The server-side files (`bridge/models.py`,
  `tunnel/token_hash.py`, `tunnel/intercept.py`) are now also in the
  perimeter (`[tool.mutmut].paths_to_mutate`), reached via the
  `src/provide/uterm/server` symlink so derived mutant module names bind
  to `provide.uterm.server...`; matching mutmut suites
  (`test_hijack_lease.py`, `test_token_hash.py`, `test_intercept_gate.py`)
  are enumerated in `tests_dir`. The earlier cross-package namespace
  collision is resolved.

## [0.4.0] — 2026-04-08

### Architecture

- **Monorepo split** into 7 focused packages: `provide-uterm` (core),
  `provide-uterm-server`, `provide-uterm-client`, `provide-uterm-platform`,
  `provide-uterm-cloudflare`, `provide-uterm-frontend`, `provide-uterm-app`.
- Namespace packaging: all packages share `provide.uterm.*` with symlinked
  source directories for development and independent PyPI distribution.

### Features

- **Character-level input filters** for BBS/telnet sessions (`consume_iac`,
  `consume_escape`) — silent IAC/ANSI discard during interactive input.
- **CF WebSocket transport adapters** (`CFWebSocketStreamReader`,
  `CFWebSocketStreamWriter`) — event-based CF Workers WebSocket ↔ asyncio
  StreamReader/StreamWriter bridge for session handlers.
- **TelnetSession enhancements** — `screen_change_seq()`,
  `wait_for_screen_change()`, public `host`/`port` attributes.
- **HTTP inspect/intercept** Playwright E2E tests (5 scenarios).
- **43 new E2E stress tests** covering chaos cascades, race conditions, fleet
  management, multi-browser hijack, broadcast storms, and session isolation.

### Resilience Hardening

- **Control channel buffer overflow protection** — configurable `max_buffer_bytes`
  (default 10 MB) prevents OOM from malformed streams.
- **Monotonic lease timing** — all internal lease/timeout comparisons use
  `time.monotonic()`, eliminating clock-skew corruption. Wall-clock `time.time()`
  reserved for user-facing timestamps only.
- **WebSocket idle timeouts** — configurable `ws_idle_timeout_s` (default 300 s)
  on both worker and browser connections prevents resource exhaustion from stale
  WebSockets.
- **EventBus sentinel guarantee** — nuclear fallback path ensures subscriber
  disconnect sentinels are always delivered, even under extreme queue pressure.
- **EventBus subscriber limits** — `max_subscribers_per_worker` (default 100)
  prevents unbounded subscription growth.
- **Graceful shutdown** — `hub.shutdown()` cancels and awaits all background
  tasks; wired into FastAPI lifespan `finally` block.
- **Debug logging in suppress blocks** — all `suppress(Exception)` blocks in
  WebSocket routes now log at DEBUG level for post-mortem diagnosis.
- **Zombie reaping in PTYConnector** — `stop()` guarantees process cleanup to
  prevent leaked child processes.

### Bug Fixes

- **Open-mode resume leak** — hijack state no longer leaks across resume tokens
  when sessions are in open input mode.
- **Rate-limit evaluation order** — global rate limits are now checked before
  per-route limits, preventing bypass via route-specific endpoints.
- **Transactional mode changes** — input mode switches are atomic with respect
  to concurrent hijack operations.
- **Tunnel reapability** — stopped tunnel sessions are now properly eligible
  for cleanup by the sweep task.
- **CF wall-clock API responses** — Cloudflare API responses use wall-clock
  timestamps (converted from internal monotonic) for consistency with browser
  expectations.

### Testing

- **100% branch + line coverage** enforced across `provide-uterm`,
  `provide-uterm-server`, and `provide-uterm-cloudflare`.
- **100% mutation kill rate** via mutmut with `--changed-only` gate.
- **7,500+ tests** across the monorepo (unit, integration, E2E, Playwright).
- **Test ordering stability** — playwright tests always run last regardless
  of collection order, preventing asyncio state corruption in combined suites.
- **SRI integrity tests** with graceful skip when frontend build artifacts
  are absent.

### Developer Experience

- Docker Compose for local development (FastAPI + CF Worker).
- Pre-commit hooks: ruff, mypy, bandit, biome, vitest, reuse (SPDX), codespell.
- Mutation testing gate in CI (`scripts/run_mutation_gate.py`).
- Quality gate script (`scripts/run_pytest_gate.py`).

## [0.3.0] — 2026-02-15

Initial public release with core terminal session management, bridge hub,
hijack leasing, recording, replay, AI/MCP integration, and Cloudflare Workers
support.
