# Changelog

All notable changes to provide-uterm are documented in this file.

## [0.5.0-dev] — 2026-04-20

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
  gate runs. Server-side files (`bridge/models.py`, `tunnel/token_hash.py`,
  `tunnel/intercept.py`) remain blocked by a cross-package namespace
  collision between mutants/ tree and uv editable installs; see
  `docs/mutmut-survivors-triage.md` for the planned fix.

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
