# uterm TypeScript Node Server Parity Design

## Purpose

Turn the TypeScript package from a rich component library with a partial Node
server into a complete portable backend. The Node server must participate in the
same executable API, WebSocket, lifecycle, authorization, and interoperability
contracts as the mature Python backend.

## Definition of complete

The Node backend serves every portable route and WebSocket surface in the
canonical operation inventory:

- session list, create, bulk delete, get, update, delete, search, connect,
  disconnect, restart, mode, clear, annotation, analysis, snapshot, events,
  event watch/stream, recordings, and session webhooks;
- worker and browser terminal WebSockets, handshake, resume, protocol framing,
  ownership, presence, limits, and cleanup;
- hijack acquire, heartbeat, snapshot, events, send, step, and release;
- fan-out group lifecycle, grants, execution modes, response collection,
  governance hold/release, and send-time authorization;
- approvals, API keys, profiles, global webhooks, graphical targets and GUI
  controls, tunnels and invites, and PAM-event ingestion;
- health, readiness, security posture, metrics, built frontend assets, and the
  remaining documented operational pages.

Native installation facilities are platform-specific, but their portable server
interfaces are not omitted. A route is complete only when it is bound, authorized,
tested through the production Node adapter, and declared in generated capabilities.

## Architecture

### Composition root

One bootstrap path owns configuration validation, durable or in-memory control
plane selection, identity and authorization policy, rate limiters, session
registry, session hubs, approval/fan-out controllers, API-key/profile/webhook
stores, graphical registry, tunnel registry, metrics, background reapers, asset
serving, and graceful shutdown.

Dependencies are injected into route and WebSocket handlers. Module-level mutable
singletons are not used for request state. Startup validates all required handlers
before listening; a missing portable handler is a startup error, not a runtime 404
or an optimistic capability.

### Canonical routing

The existing TypeScript `API_ROUTE_REGISTRY` is the canonical portable REST
inventory. A handler table must cover every registry operation exactly once, with
startup validation for missing, duplicate, or extra operation names. The few
operational endpoints outside that registry use a second explicit inventory that
is covered by the same validation and manifest generation.

Path parsing, method handling, request-size limits, content types, error envelopes,
and correlation IDs are centralized. Handlers operate on typed request context and
domain services rather than reimplementing transport concerns.

### WebSocket serving

The Node HTTP adapter owns upgrade routing and authenticates before handing a
socket to browser, worker, GUI, tunnel, event, or fan-out services. The shared
receiver preserves message boundaries, enforces the configured byte limit across
fragments, and rejects invalid control frames without exposing secrets.

The browser and worker paths use the existing control-frame codec and session-hub
domain logic. Admission is pending until initial hello/state synchronization is
complete. Ownership, generation fences, approval release, resume-token use,
replacement, disconnect, expiry, and shutdown match shared lifecycle scenarios.
Slow or failed peers are bounded and pruned independently.

### Persistence and background work

Node defaults to the existing in-memory stores for development and supports the
package's durable store abstraction wherever already defined. All stores preserve
copy/atomicity guarantees proven by the semantic fixtures. One scheduler owns
expiry for sessions, tokens, leases, approvals, API keys, graphical targets, and
tunnel invitations; shutdown stops timers and rejects new work before closing
sockets.

## Parity behavior

Python supplies the initial observable contract for authorization order, ownership
checks, status codes, stable error codes, rate-limit accounting, frame limits,
redaction, event order, and cleanup. The TypeScript server is tested against the
same fixtures and live clients. Differences require an explicit cross-backend
contract change rather than an adapter exception.

Authentication modes that are portable to Node are supported with fail-closed
configuration. Authentication runs before entity disclosure. Authorization is
rechecked at the final delivery fence for terminal input, approval release,
fan-out, GUI input, and tunnel operations.

## Failure behavior

- Invalid configuration or an incomplete handler table prevents startup.
- Invalid credentials, unavailable policy, stale ownership, exhausted quota,
  over-limit messages, bad frames, and expired resources return normalized,
  non-secret errors.
- A worker or browser failure cannot block other peers indefinitely.
- Partial fan-out, approval, replay, or tunnel delivery is reported truthfully;
  success never means merely queued or attempted.
- Shutdown drains bounded in-flight work, closes listeners and peers, stops
  background tasks, and is idempotent.

## Testing strategy

Every route family begins with an adapter-level failing test and domain behavior
is kept in focused unit tests. Contract tests enumerate the handler tables and
capabilities. Integration tests start the real Node listener on an ephemeral port
and use real HTTP and WebSocket clients. The existing multi-language live matrix
then exercises Python, Go, C#, and TypeScript clients against Node.

The TypeScript package retains 100% line, branch, statement, and function coverage.
Tests cover malformed and boundary inputs, not exclusions. Live cells may remain
unsupported only for a genuinely platform-specific operation named in the
generated manifest.

## Rollout and documentation

The partial-runtime warning remains until all required portable handlers and live
cells pass. It is then replaced atomically with Node deployment, configuration,
security, persistence, shutdown, and operational documentation. Docker and CI
smoke paths start the real server and verify health plus at least one authenticated
HTTP and WebSocket lifecycle.
