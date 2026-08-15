# Security surface parity — Python / Go / C# / Cloudflare / TypeScript

Last updated: 2026-07-31.

This matrix is the **authoritative product-scope statement** for security
controls that are not automatically identical across languages. Wire
goldens and `spec/behavior.json` cover framing/policy strings; this doc
covers **identity, authorization webhooks, SPA asset integrity, graphical
human-relay, and fan-out authorization/governance**.

**Label vocabulary** — `served`, `unserved`, `unsupported`, `partial`, `N/A`
and the `Y`/`N` qualifiers are defined once in
[`docs/parity-labels.md`](./parity-labels.md). The served server backends are
Python (FastAPI), Go, C#, and Cloudflare; TypeScript is a `partial` port and
is `unserved` for every server surface below.

| Surface | Python FastAPI | Cloudflare Worker | Go | C# | Notes |
|---|:---:|:---:|:---:|:---:|---|
| App JWT / dev_token / API key | Y | JWT only | Y | Y | Shared model |
| **CF Access email header as identity** | N (not used on FastAPI) | **Y, JWT-only** (unsigned email header **rejected**) | **N by design** | **N by design** | Never trust spoofable Access email headers |
| **CF Access verified JWT sources** | N/A (app JWT) | Y (Worker Access JWT) | **Y** (`CF-Access-JWT-Assertion` + `CF_Authorization` cookie + Bearer + app cookie) | **Y** (same order) | `cf_access_team_domain` auto-fills JWKS/issuer when empty; `jwt_default_role` for Access JWTs without roles claim |
| Local RBAC authz | Y | Y | Y | Y | viewer/operator/admin |
| **Authz decision webhook** (signed response when secret set) | Y | N/A (DO policy different) | **Y** (`WebhookAuthorizationProvider`) | **Y** (`WebhookAuthorizationProvider`) | Wired via `governance.authz_webhook_url` |
| IdP webhook + signed response | Y | N/A | Y | Y (IdP URL config) | Distinct from authz webhook |
| SPA CDN xterm **SRI** | Y (UI config) | Y (Worker SPA shell) | Y (`UI.XtermCDNIntegrity`) | **Y** (fallback shell when `Ui.XtermCdnIntegrity` set) | Self-hosted `index.html` is same-origin; SRI applies to CDN tags only |
| Human VNC relay (WS + RFB input filter) | **Y** (filter + WS route) | N | **Y** (`ServeHumanRelay` + mounted `/gui/vnc`) | **Y** (`HumanRelay` + `/gui/vnc`, RFB TCP) | Path: `WS /worker/{id}/hijack/{hid}/gui/vnc`; inject fail-closed |
| GUI inject principal-bound (`acquired_by`) | Y | N/A | Y | Y | Cross-language |
| Hijack `pending` blocks WS acquire | Y | N/A (REST hijack) | Y | Y | Cross-language |

TypeScript has no column in this table: its `partial` Node server serves none
of these controls, so every cell would read `unserved`.

## Fan-out authorization and governance

Cloudflare has no fan-out surface (`N/A`). TypeScript has a tested
route/controller module but its Node server does not mount it, so the whole
surface is `unserved` and is not a security capability of that port.

The five group operations — create, list, delete, send, and grant — are
global-admin-only. Session-scoped admins and all other authenticated
principals are rejected before request parsing or group lookup. Group access
never substitutes for current authorization to each target session.

Label definitions: [`docs/parity-labels.md`](./parity-labels.md). Every `Y` in
the TypeScript column describes the module, which no running server mounts.

| Control | Python FastAPI | Go | C# | TypeScript module |
|---|:---:|:---:|:---:|:---:|
| REST routes served by a running server | Y | Y | Y | N — unserved |
| Global admin required for create/list/delete/send/grant | Y | Y | Y | Y — unserved |
| Browser-WS fan-out send served | Y | Y | N | N |
| Unknown members rejected by default | Y | Y | Y | Y — unserved |
| Dormant-member opt-in | `fanout_allow_unknown_members` | `fanout_allow_unknown_members` | `fanout_allow_unknown_members` | component option `allowUnknownMembers` — unserved |
| Current session authz checked on every send | Y | Y | Y | Y — unserved |
| Current session authz checked on approval release | Y | N/A | N/A | Y — unserved |
| Group grantee without session access receives input | N | N | N | N |
| Configured policy may be silently bypassed | N | N | N | N |
| Policy deny / hold / release surface | served | unsupported (501, no input) | unsupported (501, no input) | unserved |
| Policy transport/error failure | fail closed | unsupported (501, no input) | unsupported (501, no input) | unserved — fails closed in module |

The dormant-member option changes creation only. A dormant ID that later
registers is still resolved and authorized against the principal who performs
the send. Revocation after group creation has the same result: that member is
reported as failed and no input or `fanout_input` observer event is emitted.

Before launching a client, the live matrix intersects each scenario's
requirements with the selected client's registered static capabilities and the
running server's announced capabilities. It validates the capabilities in the
client result again after the run. Python, Go, and C# servers announce
`fanout.rest.strict`; the TypeScript client participates through raw HTTP, but
its server announces no fan-out capability. A manually selected TypeScript
fan-out server cell is therefore reported as `unsupported` and the client is
not launched.

Policy, authorization-mutation, capture, storage, and deadline behavior is
defined by `spec/fanout_security_scenarios.json` and executed across the native
adapters by `scripts/run_fanout_security_scenarios.py`.

## Session lifecycle security

The executable lifecycle contract distinguishes a `served` route from an
authenticated explicit refusal (`unsupported`) and from a surface that is not
mounted at all (`unserved`). `unsupported (501)` below means the adapter must
observe the documented refusal through a public authenticated HTTP route; it
is not a synthetic skipped result. Label definitions:
[`docs/parity-labels.md`](./parity-labels.md).

| Scenario | Python | Go | C# | Cloudflare | TypeScript |
|---|:---:|:---:|:---:|:---:|:---:|
| Fragmented browser WebSocket message | served | served | served | served | unserved |
| Fragmented worker WebSocket message | served | served | served | served | unserved |
| Fragmented tunnel WebSocket message | served | served | served | unserved | unserved |
| Browser quota rejection, rollback, recovery | served | served | served | unsupported (501) | unserved |
| Signed governance allow | served | unsupported (501) | unsupported (501) | unsupported (501) | unserved |
| Signed governance deny | served | unsupported (501) | unsupported (501) | unsupported (501) | unserved |
| Signed governance unavailable, fail closed | served | unsupported (501) | unsupported (501) | unsupported (501) | unserved |
| Resume current owner and reject replay | served | served | served | served | unserved |
| Reject stale owner after competing acquisition | served | served | served | served | unserved |
| Reject non-owner browser step | served | served | served | served | unserved |

The source of truth is `spec/session_lifecycle_security_scenarios.json`, executed
by `scripts/run_session_lifecycle_security_scenarios.py`. Served cells must emit
normalized observations from native handlers. The `partial` TypeScript Node
server advertises none of these surfaces, so every TypeScript cell is
`unserved`. Cloudflare has no tunnel WebSocket route and does not silently
simulate per-principal quotas or signed governance.

## Intentional de-scopes (not bugs)

1. **CF Access email header on Go/C#** — Do **not** trust `Cf-Access-Authenticated-User-Email`. That header is client-forgeable without Access in front. Correct pattern: accept only cryptographically verified JWT material (`Authorization: Bearer`, `CF-Access-JWT-Assertion`, or `CF_Authorization` cookie). Regression: spoofed Access email must not change `subject_id`.

2. **Self-hosted SPA without CDN** — When the frontend is baked under `/assets` or `index.html`, Subresource Integrity for third-party CDNs is N/A. Configure CDN + integrity only when loading remote xterm.

3. **Litevirt gRPC on Python/C#** — Go dials litevirt `ProxyVNC` for human relay; C# dials RFB TCP targets; Python ships the stream relay + authz-gated WS route (backend dial may 501 for litevirt until a gRPC client is added).

## How to enable optional surfaces

### CF Access verified JWT (Go / C#)

Full worked example: `scripts/uterm-server.cf-access.example.toml`.

```toml
[auth]
mode = "jwt"
cf_access_team_domain = "myteam"
jwt_issuer = ""   # empty so team-domain auto-fill applies (default "provide-uterm" would block fill)
jwt_audience = "<your-access-app-aud>"
jwt_algorithms = ["RS256"]
jwt_default_role = "viewer"
```

Token source order (first wins): Bearer → `CF-Access-JWT-Assertion` → `CF_Authorization` cookie → `uterm_token` cookie.

C# validates RS256 via JWKS (`JwtJwksUrl` / team-domain auto-fill) or RSA public PEM; HS256 remains for `dev_token` only.

### Authz webhook (Python / Go / C#)

```toml
[governance]
authz_webhook_url = "https://policy.example.com/authz"
authz_webhook_secret = "…"   # enables require_signed_response
authz_webhook_timeout_s = 2.0
```

Responses must include `X-Uterm-Timestamp` + `X-Uterm-Signature` over the body when a secret is set (same scheme as IdP webhooks).

### C# SPA CDN SRI (fallback shell)

```toml
[ui]
xterm_cdn = "https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0"
xterm_cdn_integrity = "sha384-…"
fit_addon_cdn = "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0"
fit_addon_cdn_integrity = "sha384-…"
```

Used when `UTERM_FRONTEND_DIR` is unset; baked `index.html` is served as-is.

### Human VNC relay

```text
WS /worker/{worker_id}/hijack/{hijack_id}/gui/vnc?target_id=<graphical_target>
```

- **Python**: `filter_rfb_client_input` / `run_human_relay_streams`; FastAPI WS route (authz + lease ownership; optional `vnc_upstream_factory`)
- **C#**: `RfbInputFilter` + `HumanRelay`; ASP.NET WS route (RFB TCP dial)
- **Go**: `vnc.ServeHumanRelay` (litevirt ProxyVNC) mounted on the same path

Null / missing inject callback drops KeyEvent, PointerEvent, and ClientCutText (fail closed). Non-input client messages always pass through after the None-security handshake. Inject requires operator/admin + owned hijack lease.

## Verification

Security regressions for these surfaces live in:

- Python: `test_gui_principal_bind.py`, `test_vnc_rfb_filter.py`, `test_vnc_human_relay.py`, `test_ws_gui_vnc.py`, CF auth/webhook/SPA tests, lease pending
- Go: `serverauth` CF Access JWT + `webhook_authz_test.go`, GUI attach/ops/vnc route, lease pending, vnc filter, UI SRI
- C#: `CfAccessJwtTests`, `JwksJwtTests`, `WebhookAuthorizationTests`, `RfbInputFilterTests`, `HumanRelayTests`, GUI inject principal-bind, shell SRI
- Fan-out: `conformance/live/scenarios/010_fanout_strict_admission.json`,
  `spec/fanout_security_scenarios.json`, and
  `scripts/run_fanout_security_scenarios.py`
