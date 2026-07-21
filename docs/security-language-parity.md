# Security surface parity — Python / Go / C# / Cloudflare

Last updated: 2026-07-21.

This matrix is the **authoritative product-scope statement** for security
controls that are not automatically identical across languages. Wire
goldens and `spec/behavior.json` cover framing/policy strings; this doc
covers **identity, authorization webhooks, SPA asset integrity, and
graphical human-relay**.

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
