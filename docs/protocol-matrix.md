# Hijack Protocol Matrix

This matrix defines the backend capability contract consumed by `hijack.js`.

**Security controls that differ by language** (authz webhooks, CF Access, SPA SRI,
human VNC relay) are documented in
[`docs/security-language-parity.md`](./security-language-parity.md) — that doc is
the scope/de-scope source of truth for multi-language security parity.

## TypeScript runtime status

`packages/provide-uterm-ts` is a high-coverage partial runtime port, not yet a
third backend column for this matrix. Completed protocol, policy, hub,
connector, gateway, authentication, and configuration libraries are tested as
libraries; only these four shared HTTP capabilities are currently integrated
into its running Node server:

| Capability | TypeScript route |
|---|---|
| `sessions.list` | `GET /api/sessions` |
| `sessions.get` | `GET /api/sessions/{session_id}` |
| `sessions.snapshot` | `GET /api/sessions/{session_id}/snapshot` |
| `sessions.set_mode` | `POST /api/sessions/{session_id}/mode` |

The Node server also exposes health/liveness/readiness probes and REST hijack
lease actions, but it does not yet serve browser or worker WebSockets, the
complete session lifecycle, the hijack events poll, or every shared HTTP
route. Consequently it must not be read as supporting the FastAPI or
Cloudflare columns below.

Before TypeScript joins the Python/Go/C# multi-backend Playwright matrix, it
must serve authenticated `/ws/worker/{worker_id}/term` and
`/ws/browser/{worker_id}/term` flows with hub attachment, hello/control
frames, broadcast, and disconnect cleanup; it must also serve the fixture
lifecycle from session creation and start/attach through status, stop, and
deletion. Those surfaces need focused live integration tests before the
Playwright backend selector and CI matrix are expanded.

## Multi-session fan-out

Fan-out has a stricter security contract than ordinary group access: access to
a group never implies access to the sessions in it. Every send re-resolves each
member and checks the calling principal's current session authorization. A
revoked, deleted, or still-dormant member is reported as failed and receives no
input or observer notification.

The shared configuration key is `fanout_allow_unknown_members`. It defaults to
`false`. Setting it to `true` permits a global administrator to create a group
containing dormant IDs, but it does not weaken send-time authorization.

All five REST operations — create, list, delete, send, and grant — require an
authenticated global administrator. Session-scoped admins are rejected before
request parsing or group lookup.

| Capability | Python FastAPI | Go | C# | TypeScript |
|---|:---:|:---:|:---:|:---:|
| Fan-out REST surface served | Y | Y | Y | N (route module only) |
| Global admin required for create/list/delete/send/grant | Y | Y | Y | Y (unserved module) |
| Browser-WS fan-out send served | Y | Y | N | N |
| Reject unknown members by default | Y | Y | Y | Y (module) |
| Explicit dormant-member opt-in | Y | Y | Y | Y (module) |
| Reauthorize every member on send | Y (REST + browser WS) | Y (REST + browser WS) | Y (REST) | Y (route/controller module) |
| Reauthorize approval release | Y | N/A (no fan-out approval store) | N/A (no fan-out approval store) | Y (controller module) |
| Group grant cannot bypass session authz | Y | Y | Y | Y (module) |
| Configured governance behavior | Served webhook deny/hold/release; errors fail closed | Explicitly unsupported: deterministic 501, no input | Explicitly unsupported: deterministic 501, no input | Policy-gate module implemented; unserved |
| Parallel/sequential collection and divergence | Y | Y | Y | Y (module) |
| Live `fanout.rest.strict` server cell | Y | Y | Y | unsupported/unadvertised |

`conformance/live/scenarios/010_fanout_strict_admission.json` executes the
strict-default REST contract across the served Python, Go, and C# backends
with clients from all four languages.
The harness requires the selected client's registered static capabilities and
the running server's announcement to contain `fanout.rest.strict` before it
launches the client. It validates the returned client capabilities again after
the run. A manually selected TypeScript server therefore produces an explicit
unsupported/unserved cell before any client process is launched.
The richer cases that require mid-scenario authorization mutation, policy
infrastructure, capture lifecycle checks, store concurrency, or deadline
control are defined by `spec/fanout_security_scenarios.json` and executed by
`scripts/run_fanout_security_scenarios.py`. TypeScript deliberately does not
advertise live fan-out until the Node server mounts the route module.

## Hijack control

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| `hello.hijack_control` | `ws` | `rest` |
| `hello.hijack_step_supported` | `true` | `true` |
| `hello.mcp_supported` | `true` | `false` |
| `hello.vnc_supported` | `true` | `false` |
| Human VNC relay (browser RFB proxy) | `WS …/hijack/{id}/gui/vnc` (authz + RFB filter; upstream dial optional) | n/a |
| WS frame `hijack_request` | supported | rejected (`use_rest_hijack_api`) |
| WS frame `hijack_release` | supported | rejected (`use_rest_hijack_api`) |
| WS frame `hijack_step` | supported | rejected (`use_rest_hijack_api`) |
| REST `/hijack/acquire` | supported | supported |
| REST `/hijack/{id}/heartbeat` | supported | supported |
| REST `/hijack/{id}/release` | supported | supported |
| REST `/hijack/{id}/step` | supported | supported |
| REST `/hijack/{id}/send` | supported | supported |
| REST `/hijack/{id}/snapshot` | supported | supported |
| REST `/hijack/{id}/events` | supported | supported |

## Session resumption

Opt-in feature. Enabled on FastAPI by passing `resume_store` to `TermHub`; always enabled on the CF backend (SQLite-backed).

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| `hello.resume_supported` | `true` when store configured, else absent | `true` always |
| `hello.resume_token` | opaque token (256-bit, urlsafe) | opaque token (256-bit, urlsafe) |
| `hello.resumed` | `true` on successful resume | `true` on successful resume |
| WS frame `{"type":"resume","token":"…"}` | supported (first message after connect) | supported (any browser message) |
| Token TTL | configurable via `resume_ttl_s` (default 300s) | configurable via `resume_ttl_s` (default 300s) |
| Token storage | `InMemoryResumeStore` (default) or pluggable | DO SQLite `resume_tokens` table |
| Token lifetime after disconnect | preserved until TTL | preserved until TTL |
| Invalid/expired token behavior | silently ignored, fresh session stands | silently ignored, fresh session stands |
| Hijack ownership recovery | yes, if lease still active and no new owner | yes, if lease still active and no new owner |
| Browser storage | `sessionStorage` keyed by `uterm_resume_{worker_id}` | same |

## Client behavior contract

- The client must key behavior on `hello.hijack_control` (or `hello.capabilities.hijack_control`).
- The client must not assume backend type by URL or deployment.
- Unsupported WS control paths must degrade to REST when `hijack_control=rest`.
- If `hello.resume_supported` is `true` and a stored token exists, the client must send `{"type":"resume","token":"…"}` as its first message after connect.
- The client must update its stored token on every hello (initial and resumed) — tokens are rotated on each resume.
- FastAPI resume tokens are opaque session handles. By default they restore the
  prior browser role from the token; consumers that need identity-aware resume
  checks must provide `on_resume` validation when constructing `TermHub`.

## Tunnel protocol

Binary multiplexed WebSocket framing for terminal sharing, TCP forwarding, and HTTP inspection.

### Wire format

`[1 byte channel][1 byte flags][N bytes payload]` per binary WebSocket message.

| Channel | Name | Payload | CLI command |
|---------|------|---------|-------------|
| `0x00` | Control | JSON: `open`, `resize`, `close`, `error` | all |
| `0x01` | Terminal | Raw PTY bytes or log lines | `uterm share`, `uterm inspect` |
| `0x02` | TCP | Raw TCP bytes | `uterm tunnel` |
| `0x03` | HTTP | Structured JSON: `http_req`, `http_res` | `uterm inspect` |

Flags: `0x00` = data, `0x01` = EOF (half-close).

### Tunnel endpoints

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| Agent endpoint | `WSS /tunnel/{worker_id}` | `WSS /tunnel/{tunnel_id}` (via DO) |
| Browser endpoint | `WSS /ws/browser/{id}/term` | same |
| `POST /api/tunnels` | supported | supported |
| `DELETE /api/tunnels/{id}/tokens` | supported (revocation) | supported (revocation) |
| `POST /api/tunnels/{id}/tokens/rotate` | supported (rotation) | supported (rotation) |
| Share URL (`?invite=...`) | `/s/{id}` → set HttpOnly cookie, 302 clean redirect | `/s/{id}` → set HttpOnly cookie, 302 clean redirect |
| Inspect view | `/app/inspect/{id}` | `/app/inspect/{id}` |

### Tunnel auth

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| Agent auth | Global `worker_bearer_token` OR per-session `worker_token` | same |
| Share token | `uterm_tunnel_{id}` cookie only after invite bootstrap | `uterm_tunnel_{id}` cookie only after invite bootstrap |
| Control token | `uterm_tunnel_{id}` cookie only after invite bootstrap | `uterm_tunnel_{id}` cookie only after invite bootstrap |
| Token TTL | Default 1h, configurable via `TunnelConfig.token_ttl_s` | Default 1h, configurable via `TUNNEL_TOKEN_TTL_S` env var |
| Token revocation | `DELETE /api/tunnels/{id}/tokens` | `DELETE /api/tunnels/{id}/tokens` |
| Token rotation | `POST /api/tunnels/{id}/tokens/rotate` | `POST /api/tunnels/{id}/tokens/rotate` |
| IP binding | Optional (`TunnelConfig.ip_binding`) | Optional (`TUNNEL_IP_BINDING` env var) |
| Timing-safe compare | `secrets.compare_digest()` | same |
| Enumeration prevention | 404 for both "not found" and "invalid token" | same |

### HTTP inspection (channel 0x03)

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| Channel 0x03 broadcast | `hub.broadcast()` + `hub.append_event()` | `runtime.broadcast_worker_frame()` |
| HTTP req/res JSON | Parsed, tagged `_channel: "http"`, broadcast to browsers | same |
| Invalid JSON handling | Logged as warning, dropped | same |
| Body < 256KB | Included as `body_b64` (base64) | same (agent-side encoding) |
| Body > 256KB | `body_truncated: true`, no `body_b64` | same |
| Binary content | `body_binary: true`, no `body_b64` | same |
| Inspect view | `/app/inspect/{id}` — live request list + detail | same |

## Health checks

Unauthenticated endpoints for load balancers, orchestrators, and monitoring.

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| `GET /api/health` | supported (no auth) | supported (no auth) |
| Response: `status` | `"ok"` or `"unavailable"` | `"ok"` (via `ok: true`) |
| Response: `version` | package version string | not included |
| Response: `uptime_s` | seconds since server start | not included |
| Response: `active_sessions` | count from session registry | not included |
| Response: `control_plane_backend` | `"memory"` or `"sqlite"` | not included |
| `GET /healthz` | supported (minimal, no deps) | not supported |
| Auth required | no | no |

## Security headers

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| `security.mode` | `"strict"` / `"dev"` (SecurityConfig) | `SECURITY_MODE` env var |
| Content-Security-Policy | strict: full CSP; dev: not set | same |
| Strict-Transport-Security | strict: `max-age=63072000; includeSubDomains`; dev: not set | same |
| X-Frame-Options | strict: `DENY`; dev: not set | same |
| X-Content-Type-Options | always `nosniff` | same |
| Referrer-Policy | strict: `strict-origin-when-cross-origin`; dev: not set | same |
| Permissions-Policy | strict: `camera=(), microphone=(), geolocation=()`; dev: not set | same |
| Per-header override | config field (None=default, ""=suppress, "value"=custom) | env var (same semantics) |
| SRI on CDN assets | `integrity` + `crossorigin` on all jsdelivr script/link tags | same |
| WebSocket 101 bypass | headers not applied to WS upgrades | same |

## DeckMux (collaborative presence)

Real-time collaborative presence for terminal sessions. Enabled per session with `presence: true`.

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| Session config: `presence` | `SessionDefinition.presence` | KV session entry |
| `presence_update` relay | TermHub broadcast | DO broadcast |
| `presence_sync` on join | sent from hub mixin | sent from DO |
| `presence_leave` on disconnect | sent from hub mixin | sent from DO |
| `control_request` / `control_transfer` | via hijack lease system | via DO lease state |
| Auto-transfer (idle owner) | background check in hub | event-driven in DO |
| Keystroke queue | in-memory buffer | in-memory buffer |
| Hibernation recovery | N/A (always running) | ephemeral re-announce |
| Identity (JWT users) | from principal claims | from JWT claims |
| Identity (anonymous) | deterministic adjective+animal | same |
| Edge indicators | frontend-only | same |
| Name labels toggle | frontend-only | same |

### DeckMux message types

| Direction | Type | Payload |
|---|---|---|
| Browser -> Server | `presence_update` | `scroll_line`, `scroll_range`, `selection`, `pin`, `typing` |
| Browser -> Server | `queued_input` | `keys` (buffered keystrokes from non-owner) |
| Browser -> Server | `control_request` | `target` (user to request control from) |
| Server -> Browser | `presence_update` | `user_id`, `name`, `color`, `role`, scroll/selection/pin state |
| Server -> Browser | `presence_sync` | `users` (full state array), `config` |
| Server -> Browser | `presence_leave` | `user_id` |
| Server -> Browser | `control_transfer` | `from_user_id`, `to_user_id`, `reason`, `queued_keys` |
| Server -> Browser | `auto_transfer_warning` | `seconds_remaining` |

These are the seven message types defined in
`packages/provide-uterm/src/provide/uterm/deckmux/_protocol.py`. Control is
*requested* via a `control_request` and *granted* via a single
`control_transfer` whose `reason` is one of `handover`, `auto_idle`,
`admin_takeover`, or `lease_expired`; there are no separate
handover/deny/notification/denied message types — those flows are handled by the
hijack lease system. `auto_transfer_warning` is the only idle-warning
Server -> Browser message.

All messages use the existing control channel (DLE+STX JSON framing). 200ms client-side debounce on presence updates. The deckmux module is part of the `provide-uterm` core package.

## Accuracy note

This document describes the intended public contract. It does not mean every
backend edge case is perfectly identical today. In particular, verify auth and
lease-validation behavior against current tests before treating the two
backends as interchangeable for security-sensitive flows.
