# HANDOFF — Live tests for the kbdint + webhook-crypto paths

## Problem / Request

Two security-relevant code paths were hardened in earlier work but lacked an
end-to-end ("live") test that drives them through their real runtime rather than
a mock:

1. **SSH keyboard-interactive (kbdint) auth** in `SshWsGateway`
   (`gateway/_ssh_handler.py`) — `kbdint_auth_supported()` /
   `get_kbdint_challenge()` / `validate_kbdint_response()`. The gateway advertises
   kbdint; before these overrides existed a client that selected kbdint failed
   silently. Needed: a real asyncssh client forced onto the kbdint path that
   proves the handshake completes.

2. **Webhook secret encryption at rest** in the Cloudflare DO
   (`do/_webhook_crypto.py` → `encrypt_secret` / `decrypt_secret`, AES-256-GCM via
   `crypto.subtle`). Those functions are `# pragma: no cover - CF runtime only`
   because Web Crypto only exists inside workerd/Pyodide — a unit test cannot
   reach them. Needed: an e2e test that drives the register path **with a secret**
   so the encrypt branch runs in the real Worker.

Request: **"set things up for a live test"** for both. The kbdint one is fully
verifiable locally; the webhook-crypto one runs against a local `pywrangler dev`
worker (workerd) and is fully validated end-to-end only against a deployed Worker
with `WEBHOOK_SECRET_KEY` configured — hence the manual round-trip runbook below.

## Changes completed

1. **kbdint e2e test** (`7a97987a`) —
   `packages/provide-uterm-server/tests/e2e/test_ssh_gateway_start.py`,
   `TestSshWsGatewayStart::test_keyboard_interactive_auth_completes_handshake`.
   Connects a real asyncssh client restricted to `preferred_auth=
   "keyboard-interactive"` (no pubkey: `client_keys=[]`) against a live
   `SshWsGateway`, and asserts the upstream WS banner (`KBDINT-OK`) reaches the
   SSH `create_process` stdout — i.e. the kbdint handshake completed. Reaching the
   process at all proves the empty challenge was accepted.
   **Verified: 8/8 passing locally.**

2. **webhook register-with-secret e2e test** —
   `packages/provide-uterm-cloudflare/tests/test_e2e_sse_webhooks.py`,
   `test_do_webhook_register_with_secret_does_not_echo_secret`. Registers a
   webhook with a `secret`, asserting 200 + webhook_id and that the plaintext
   secret never round-trips in the response. Driving register with a secret runs
   `encrypt_secret` in the real workerd runtime (the only place `crypto.subtle`
   exists). `@pytest.mark.e2e` — skipped by default, runs under `E2E=1`/`-m e2e`.

## Reasoning

- **kbdint client config gotcha:** `preferred_auth="keyboard-interactive"` alone
  gives asyncssh's default client no kbdint *response source* → "Permission
  denied". asyncssh's default client uses the `password=` value as its kbdint
  response; the gateway issues an empty (no-prompt) challenge so the value is
  unused — passing `password="x"` only *enables* the client to attempt kbdint at
  all. This is documented inline in the test.
- **Why the webhook test only asserts no-echo, not a full decrypt:** webhook
  delivery is fire-and-forget (the DO decrypts + HMAC-signs + POSTs out of band),
  so a client cannot synchronously observe the decrypted secret or the signature.
  The register path is the part that runs `encrypt_secret` and is observable. The
  full `encrypt → decrypt → HMAC` round-trip is validated manually (runbook below).
- The no-echo assertion holds **with or without** `WEBHOOK_SECRET_KEY` set: when
  the key is unset the crypto module returns plaintext (documented fallback), but
  the response still must not echo the secret. So the test is meaningful in both
  local (`pywrangler dev`, key usually unset) and deployed (key set) runs.

## How to run the live tests (runbook)

### 1. kbdint SSH gateway (fully local, no external infra)

```bash
uv run pytest \
  packages/provide-uterm-server/tests/e2e/test_ssh_gateway_start.py \
  -k keyboard_interactive -vv
```

Spins up an in-process WS echo server + a real `SshWsGateway` on an ephemeral
port and connects a real asyncssh client. Passes in ~1s. No env vars required.

### 2. webhook secret encryption (local workerd)

```bash
# Runs encrypt_secret in workerd. With no WEBHOOK_SECRET_KEY the crypto module
# falls back to plaintext-at-rest, but the no-echo assertion still holds.
E2E=1 uv run pytest \
  packages/provide-uterm-cloudflare/tests/test_e2e_sse_webhooks.py \
  -k register_with_secret -vv
```

The `wrangler_server` session fixture boots `pywrangler dev` (slow — 90s startup
budget) and the test is `skip`-gated unless `E2E=1` / `-m e2e`.

**Local-dev caveat (verified 2026-06-06):** in a plain checkout `pywrangler dev`
fails to boot because `wrangler.toml`'s `main = ".../cloudflare/entry.py"` points
at a Worker entry file that is **not present** — the former monolithic `entry.py`
was split into the `entry/` package, and the single-file `entry.py` the Worker
loads is a deploy/build artifact this checkout doesn't generate (wrangler's only
`[build]` hook is the frontend `cp`; `uterm-cf build` merely *validates* layout).
The fixture then `skip`s with "pywrangler dev did not start within 90s". This
affects **every** `@pytest.mark.e2e` webhook test equally — not just the new one.
So local-workerd validation requires first producing that `entry.py` (assemble
from `entry/` or run a deploy build); otherwise use the deployed-Worker path:

```bash
REAL_CF=1 REAL_CF_URL=https://provide-uterm-cloudflare.neurotic.workers.dev \
  uv run pytest -m e2e \
  packages/provide-uterm-cloudflare/tests/test_e2e_sse_webhooks.py \
  -k register_with_secret -vv
```

### 3. webhook FULL encrypt→decrypt→HMAC round-trip (deployed Worker)

The at-rest ciphertext and the outbound `x-uterm-signature` are not observable
from the register response, so the real crypto round-trip is validated manually
against a Worker that has the AES key configured:

1. **Generate a 256-bit key (base64):**
   ```bash
   uv run python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
   ```
2. **Set it as the Worker secret** (do **not** commit it — `WEBHOOK_SECRET_KEY` is
   intentionally absent from `wrangler.toml` `[vars]`):
   ```bash
   # deployed:
   cd packages/provide-uterm-cloudflare && uv run pywrangler secret put WEBHOOK_SECRET_KEY
   # local dev: create packages/provide-uterm-cloudflare/.dev.vars (gitignored) with
   #   WEBHOOK_SECRET_KEY="<the base64 key>"
   ```
3. **Register a webhook** pointing at a request bin (e.g. https://webhook.site):
   ```bash
   curl -sS -X POST "$BASE/api/sessions/$WID/webhooks" \
     -H 'content-type: application/json' \
     -d '{"url":"https://webhook.site/<your-bin>","secret":"<round-trip-secret>","pattern":".*"}'
   ```
4. **Trigger a delivery** (drive the session so the DO fires the webhook), then on
   webhook.site confirm the inbound request carries
   `x-uterm-signature: sha256=<hmac>`. Recompute locally to confirm decrypt worked:
   ```bash
   uv run python -c 'import hmac,hashlib,sys; \
     body=sys.argv[2].encode(); \
     print("sha256="+hmac.new(sys.argv[1].encode(), body, hashlib.sha256).hexdigest())' \
     round-trip-secret '<the exact delivered body>'
   ```
   A match proves `decrypt_secret` recovered the original secret (so `encrypt_secret`
   wrote valid AES-256-GCM ciphertext at rest).

To run the e2e suite against a deployed Worker instead of local dev:
`REAL_CF=1 REAL_CF_URL=https://<worker-host> uv run pytest -m e2e ...`.

## Summary of work done

- Added a real-client kbdint handshake e2e test (committed `7a97987a`, 8/8 green).
- Added a workerd-backed webhook register-with-secret e2e test that exercises
  `encrypt_secret` and asserts the plaintext secret never leaks in the response.
- Documented the full local + deployed runbook for both, including the manual
  HMAC round-trip that proves the AES-256-GCM decrypt path (which cannot be
  observed synchronously from a client).

## Checklist for next session

Current repo status as of 2026-06-09:

- [x] uterm consumer API gaps Part A completed on local `main` through
      `d5602c0b` (`Implement uterm consumer API gaps`).
- [x] Post-commit verification passed: `make quality-gate` and
      `uv run python scripts/run_all_tests.py`.
- [x] Subagent task branches for U1-U8 have no unique commits relative to
      `main`; their work is represented by the merged local commits.
- [ ] Push local `main` when ready. At this checkpoint it is ahead of
      `origin/main`; no push was performed in the API-gap implementation turn.

- [x] Local `E2E=1 … -k register_with_secret` run: **SKIPs** in a plain checkout
      (pywrangler dev can't boot without the `entry.py` Worker artifact — see the
      "Local-dev caveat" above). The test/ruff/LOC are all green and it collects
      cleanly; authoritative pass requires the deployed-Worker `REAL_CF` path or a
      locally-assembled `entry.py`.
- [ ] Run the deployed-Worker `REAL_CF` path once a Worker with (optionally)
      `WEBHOOK_SECRET_KEY` is reachable, to get a real green on this test.
- [x] Committed the webhook e2e test (the runbook lives here in the gitignored
      `.provide/HANDOFF.md`, per repo convention — not part of the commit).
- [ ] Push `main`: `7a97987a` (kbdint) is local-only; push it together with the
      webhook test commit. `origin/main` was at `866f199f` at hand-off.
- [ ] Optional: run the deployed-Worker round-trip (step 3 above) once a Worker
      with `WEBHOOK_SECRET_KEY` is available, to validate the decrypt+HMAC path.
- [ ] Never run a foreground `uv` command while a gate/e2e run is in the
      background — both invoke `uv sync` and race on `.venv` (`exit 2`).
