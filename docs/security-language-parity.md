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
| **CF Access email header as identity** | N (not used on FastAPI) | **Y, JWT-only** (unsigned email header **rejected**) | **N by design** | **N by design** | Self-hosted ports must **not** trust spoofable Access headers. Use Access as OIDC → mint app JWT. |
| Local RBAC authz | Y | Y | Y | Y | viewer/operator/admin |
| **Authz decision webhook** (signed response when secret set) | Y | N/A (DO policy different) | **Y** (`WebhookAuthorizationProvider`) | N (local authz only) | Go wired via `governance.authz_webhook_url` |
| IdP webhook + signed response | Y | N/A | Y | Y (IdP URL config) | Distinct from authz webhook |
| SPA CDN xterm **SRI** | Y (UI config) | Y (Worker SPA shell) | Y (`UI.XtermCDNIntegrity`) | **Y** (fallback shell when `Ui.XtermCdnIntegrity` set) | Self-hosted `index.html` is same-origin; SRI applies to CDN tags only |
| Human VNC relay (WS→litevirt RFB filter) | N | N | **Y** (`ServeHumanRelay`) | N | `hello.vnc_supported` ≠ human-relay route; Go-only until ported |
| GUI inject principal-bound (`acquired_by`) | Y | N/A | Y | Y | Cross-language |
| Hijack `pending` blocks WS acquire | Y | N/A (REST hijack) | Y | Y | Cross-language |

## Intentional de-scopes (not bugs)

1. **CF Access on Go/C#** — Do not port “trust `Cf-Access-Authenticated-User-Email`”. That header is client-forgeable without Access in front. Correct pattern: Cloudflare Access (or any IdP) issues a **verified JWT**; Go/C# authenticate the JWT only. Regression: spoofed Access email must not change `subject_id`.

2. **C# authz webhook** — Not required for local RBAC deployments. Prefer Go/Python when external policy engines are needed, or add later with the same signed-response contract.

3. **Human VNC relay** — Implemented in Go only. Python GUI attach is memory-only by default; C# uses headless RFB client for REST inject, not a browser RFB proxy path.

4. **Self-hosted SPA without CDN** — When the frontend is baked under `/assets` or `index.html`, Subresource Integrity for third-party CDNs is N/A. Configure CDN + integrity only when loading remote xterm.

## How to enable optional surfaces

### Go authz webhook

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

## Verification

Security regressions for these surfaces live in:

- Python: `test_gui_principal_bind.py`, CF auth/webhook/SPA tests, lease pending
- Go: `serverauth/webhook_authz_test.go`, GUI attach/ops, lease pending, vnc filter, UI SRI
- C#: `EgressGuardTests`, `DetachClientTests`, `McpAuthTests`, `Gui_Inject_Denied_When_AcquiredBy_Mismatch`, shell SRI unit tests
