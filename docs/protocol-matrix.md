# Hijack Protocol Matrix

This matrix defines the backend capability contract consumed by `hijack.js`.

## Hijack control

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| `hello.hijack_control` | `ws` | `rest` |
| `hello.hijack_step_supported` | `true` | `true` |
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
| Share URL (`?token=...`) | `/s/{id}` → 302 redirect | `/s/{id}` → 302 redirect |
| Inspect view | `/app/inspect/{id}` | `/app/inspect/{id}` |

### Tunnel auth

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| Agent auth | Global `worker_bearer_token` OR per-session `worker_token` | same |
| Share token | Query param or `uterm_tunnel_{id}` cookie | Query param or `uterm_tunnel_{id}` cookie |
| Control token | Query param or cookie | Query param or cookie |
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
| Browser -> Server | `control_handover` | `to` (user to hand control to) |
| Browser -> Server | `control_deny` | `requester` (user whose request is denied) |
| Server -> Browser | `presence_update` | `user_id`, `name`, `color`, `role`, scroll/selection/pin state |
| Server -> Browser | `presence_sync` | `users` (full state array), `config` |
| Server -> Browser | `presence_leave` | `user_id` |
| Server -> Browser | `control_transfer` | `from_user_id`, `to_user_id`, `reason`, `queued_keys` |
| Server -> Browser | `control_request_notification` | `from` (requesting user) |
| Server -> Browser | `control_denied` | (empty) |
| Server -> Browser | `auto_transfer_warning` | `seconds_remaining` |

All messages use the existing control channel (DLE+STX JSON framing). 200ms client-side debounce on presence updates. The deckmux module is part of the `provide-uterm` core package.

## Accuracy note

This document describes the intended public contract. It does not mean every
backend edge case is perfectly identical today. In particular, verify auth and
lease-validation behavior against current tests before treating the two
backends as interchangeable for security-sensitive flows.
