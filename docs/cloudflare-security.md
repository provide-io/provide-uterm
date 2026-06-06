# Cloudflare Worker — CSRF / cross-origin hardening

The Cloudflare Durable Object backend authenticates browser requests with the
`CF_Authorization` cookie (`auth/jwt.py::extract_bearer_or_cookie`). Because a
cookie is an *ambient* credential, state-changing routes need explicit
cross-site-request-forgery (CSRF) defenses. This note records the defense model
and the one setting that must be verified outside this repo.

## Operator action — verify `CF_Authorization` is `SameSite=Lax`

`CF_Authorization` is issued and its attributes (including `SameSite`) are set by
**Cloudflare Access**, not by this application — the worker only *reads* it. The
strongest single mitigation is to ensure Cloudflare Access issues it as
`SameSite=Lax` (the default for the app's own cookies, e.g.
`entry/share_tokens.py`), so browsers never attach it to a cross-site `POST`/
`fetch`. Confirm this in the Cloudflare Access / Zero Trust configuration for the
deployment. **The code defenses below do not depend on this** — they hold even if
Cloudflare issues the cookie `SameSite=None` — but verifying it is defense in
depth and removes the entire class of attack at the edge.

## Code defenses (do not rely on the cookie's SameSite)

1. **Per-lease nonce on the highest-risk route.** Keystroke injection
   (`POST .../hijack/{hijack_id}/send`) requires the active `hijack_id` — a
   `uuid.uuid4()` minted server-side on `acquire`, returned only to the
   legitimate admin and never broadcast. An attacker cannot guess it or read it
   cross-origin (no CORS), so they cannot construct the URL. This already blocks
   the headline "CSRF → keystroke RCE" vector.

2. **Cross-site request guard** (`api/http_routes/_dispatch.py::_is_cross_site`).
   Every state-changing method (`POST`/`PUT`/`PATCH`/`DELETE`) is rejected with
   `403 cross_site_blocked` when it looks like a cross-site *browser* request —
   primary signal `Sec-Fetch-Site: cross-site` (sent by all modern browsers),
   `Origin`-vs-host as the fallback, opaque `Origin: null` treated as cross-site.
   Non-browser clients (CLI, worker, server-to-server) send neither header and
   are unaffected — CSRF requires an ambient browser cookie they do not carry.
   This closes the nonce-less admin routes (`hijack/acquire`, `input_mode`,
   `disconnect_worker`).

3. **Content-Type enforcement** (`do/session_runtime/io.py::request_json`).
   Request bodies are parsed only when `Content-Type` is `application/json`. A
   CSRF "simple request" must use `text/plain`/form encodings to skip the CORS
   preflight; requiring `application/json` forces a preflight that the worker
   (which exposes no permissive CORS handler) fails — so the forged `POST` is
   never sent.

These are layered: (1) protects the RCE path structurally, (2) is the general
guard for all mutating routes, and (3) is belt-and-suspenders that also forces a
preflight. None requires CORS handlers or anti-CSRF tokens to be added.
