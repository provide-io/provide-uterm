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

Binary WebSocket framing for `uterm share` terminal sharing and future TCP/HTTP tunneling.

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| Wire format | `[1B channel][1B flags][N bytes payload]` | same |
| Channel 0x00 (control) | JSON: open, resize, close, snapshot | same |
| Channel 0x01 (data) | Raw PTY bytes | same |
| Flag 0x01 (EOF) | Half-close signal | same |
| Endpoint | `WSS /tunnel/{worker_id}` | `WSS /tunnel/{tunnel_id}` (via DO) |
| Auth (agent) | `worker_bearer_token` (Bearer header) | Per-session `worker_token` in KV |
| `POST /api/tunnels` | not yet | supported (creates session + tokens) |
| `GET /s/{id}?token=…` | not yet | supported (share/control token → role) |

## Accuracy note

This document describes the intended public contract. It does not mean every
backend edge case is perfectly identical today. In particular, verify auth and
lease-validation behavior against current tests before treating the two
backends as interchangeable for security-sensitive flows.
