# Design: Deprecate `auth.mode="dev"` / `"none"`

## Problem

`auth.mode` accepts `"dev"` and `"none"` values that disable authentication entirely (see `packages/provide-uterm-server/src/provide/uterm/server/app/auth.py:74-97`). The current safeguards are: (1) refuse to start if `require_jwt_in_production=true`, and (2) refuse to start if `server.host` is not a loopback address. Any caller that can reach loopback (container sidecar, ssh tunnel, host-mode pod, supply-chain attacker with code exec) silently bypasses auth and can claim any principal/role via `X-Principal` / `X-Role`. The startup warning at `auth.py:92-96` is the only runtime signal.

## Options

| Option | Pro | Con |
|---|---|---|
| **A. Remove `dev`/`none` entirely.** Tests must run with `mode="jwt"` plus a fixture-generated keypair. | Eliminates the foot-gun. One fewer auth code path to maintain (the `mode in {"none","dev"}` short-circuit at `auth.py:76`). | Breaking change for `docker/server.toml`, example configs, and every test using `dev`. Cost spread across the test base. |
| **B. Keep `dev` but require explicit `--i-know-this-is-dangerous` CLI flag** plus louder warning. | Minimal code churn. Trips up casual misuse. | Anyone copy-pasting a config still hits the same trap; warnings can be ignored. Doesn't address sidecar reachability. |
| **C. Tighten to bind-only-on-127.0.0.1.** Refuse to start unless `server.host in {"127.0.0.1","::1"}` (currently `_LOOPBACK_HOSTS` also accepts `"localhost"`, which resolves variably). Optionally also gate on UID matching. | Smallest diff. Closes the `localhost`-resolves-to-`0.0.0.0` foot-gun on misconfigured DNS. | Sidecar/tunnel bypass still works. Doesn't help containerized dev. |
| **D. Replace `dev` with a stub-IdP `dev_token` mode.** On startup in dev, mint a short-lived JWT, write it to a 0600 file, and require all requests to present it. Behaves like `jwt` but with auto-provisioning. | Auth code paths collapse to one (always-JWT). Token file ACL provides the loopback-equivalent boundary. | Implementation work: stub key generation, token rotation, doc updates. Local CLI clients must read the token file. |

## Recommendation

**D, staged behind A.** Start by adding the stub-IdP path so `mode="dev"` becomes a thin shim that auto-issues a JWT and forwards through the `jwt` validator. Once tests and example configs migrate, drop the `mode in {"none","dev"}` branch at `auth.py:76` entirely. This removes the "anyone-on-loopback-is-root" code path while keeping the developer ergonomics of a one-line config.

If maintainers want a smaller diff first: **C** is a cheap immediate hardening — replace `_is_loopback_host` permissiveness with strict numeric IPs.

## Files that would change

- `packages/provide-uterm-server/src/provide/uterm/server/app/auth.py:74-97` — remove the `none/dev` branch, or replace with stub-IdP token issuance.
- `packages/provide-uterm-server/src/provide/uterm/server/app/auth.py:30-44` (`_LOOPBACK_HOSTS`, `_is_loopback_host`) — tighten or delete depending on chosen option.
- `packages/provide-uterm-server/src/provide/uterm/server/config.py` (auth section, search for `mode:` default) — change default, add `dev_token_path` if pursuing D.
- `docker/server.toml`, `scripts/uterm-server.example.toml` — update example.
- Tests under `packages/provide-uterm-server/tests/` that pass `auth.mode="dev"` — search and migrate.
- `CLAUDE.md` "Auth modes" line — update doc.

## Open question for maintainer

Does any production deployment rely on `mode="header"` behind a reverse proxy (see `auth.py:98-108`)? If yes, option D needs to coexist with header mode; if no, the same retirement argument applies to `header`.
