# Changelog

All notable changes to provide-uterm are documented in this file.

## [0.5.0-dev] — 2026-04-20

### Architecture

- **Pluggable Authorization Service:** Refactored `AuthorizationService` into an async, protocol-based gateway. Supports `LocalAuthorizationProvider` (AGPL default) and `WebhookAuthorizationProvider` (managed integration).
- **Extensible Policy Gates:** Introduced `PolicyGate` (Terminal Hub) and `AgentSpawnPolicyGate` (Agent Manager) to intercept real-time input and process spawning.
- **Standardized Telemetry (DAS):** Instrumented core components with the `provide-telemetry` Domain-Action-Status (DAS) schema for high-fidelity structured logging.
- **Node Discovery:** Implemented background heartbeat for automated Node registration with external External Management Tiers.
- **Legacy Cleanup:** Purged 3,000+ lines of legacy vanilla JavaScript frontend; Node now strictly serves the React/Vite dashboard. Simplified API key auth by removing role-marker scope shorthand.

## [0.4.0] — 2026-04-08

### Architecture

- **Monorepo split** into 7 focused packages: `provide-uterm` (core),
  `provide-uterm-server`, `provide-uterm-client`, `provide-uterm-platform`,
  `provide-uterm-cloudflare`, `provide-uterm-frontend`, `provide-uterm-app`.
- Namespace packaging: all packages share `provide.terminal.*` with symlinked
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
