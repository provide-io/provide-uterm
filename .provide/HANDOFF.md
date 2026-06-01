# Handoff: Enterprise Hardening & Reliability Program

_Last updated: 2026-06-01 (verify-remediation wave + ws_transport.py adoption). Supersedes the prior hardening handoff._

## Problem / request

"Perform a comprehensive code review and detailed architectural analysis … It is vital that this is
'enterprise hardened' and 'reliable.'" Executed as a review → remediate → verify → re-review → re-remediate
program. The latest milestone closed the four design-decision items, added a unified environment profile,
then ran an **independent multi-agent re-verification of the whole hardening body** which found real
shipped gaps — now remediated.

## Approach / operating contract (keep following it)

- **Per item:** TDD (test first, RED → implement → GREEN) → **independent adversarial subagent review**
  (sonnet) → appropriate **gate** → **fast-forward merge** to local `main`. For a confirmed vulnerability,
  the first test PROVES the exploit (RED) before the fix closes it.
- **Gating:** leaf change → that package's suite; **cross-cutting** (auth, egress, redaction, config
  validation, lifespan, control-plane) → **full** `uv run python scripts/run_all_tests.py`. NEVER trust the
  wrapper exit code — grep the output for `FAILED` / `not reached`. Exclude the flaky memray test by
  **node-id**: `--deselect "packages/provide-uterm/tests/memray/test_event_bus_stress.py::test_event_bus_stress"`.
  **Do NOT pass a global `-m`** to `run_all_tests.py` (it clobbers each suite's marker selection → mass false
  failures).
- **Commits:** one logical unit per commit; conventional messages; **NO AI/Claude trailers**; subagents
  stage only their own files (**no `git add -A`**). **No git rollbacks/resets** (auto-commit env).
- Subagent pattern that has held at ~100% first-pass approval: implementer (TDD) → adversarial reviewer
  (verifies the exploit is closed + nothing weakened) → controller runs the gate → ff-merge.

## Working-tree notes for the next session

- **Stray WIP from another tool — RESOLVED (committed with tests).** The formerly-untracked
  `transports/ws_transport.py` was adopted and hardened for the websockets 16.0 API and **committed with
  its test suite** (`8e9840e4` *feat(client): harden WebSocketTransport for websockets 16.0*, plus the
  `b2b54e91` test tidy; test at `packages/provide-uterm-client/tests/transports/test_ws_transport.py`).
  `packages/provide-uterm/.../ws_session.py` is likewise now tracked (with
  `tests/terminal/test_ws_session.py`), and the lazy `transports/__init__.py` `__getattr__` edit is
  committed. The 0%-coverage gate failure that dropped the client package to 95.82% no longer applies —
  the working tree is clean for these files. (Task #35 *Adopt + harden ws_transport.py* and #36 *shared
  TransportSession base* are both completed.)
- **Known pre-existing flake (NOT from this work):**
  `tests/bridge/hub/test_limiter.py::test_send_eviction_never_drops_the_inserting_client` failed once in the
  full server suite under the random seed, but passes 3/3 in isolation and 18/18 in its file in random order;
  this branch does not touch `limiter.py`, and 3 earlier full-server runs (4870/4900/4921) passed. It is a
  cross-test-pollution flake (shared RateLimiter/clock state from an earlier test in the random order) — a
  reliability follow-up worth a focused de-pollution pass, but unrelated to the hardening changes.

## Work completed (all merged to local `main`; ~51 commits ahead of origin, UNPUSHED by user's choice)

- **Design-decision items (task #19) — DONE:**
  - **1f/1d** webhook-IdP contract: verify the IdP *response* signature (`webhook_idp_require_signed_response`,
    default on) + curate the headers/cookies forwarded to the IdP (allow-list).
  - **5a** FULL WORM/compliance audit build (3 sub-tasks): tamper-evident sha256 hash-chain + monotonic seq +
    append-only `O_NOFOLLOW`/0600 fsync file sink + `uterm audit verify` CLI; durable control-plane chain-head
    with a monotonic anti-rollback guard (sqlite migration v0002); lifespan wiring (resume + startup
    integrity alarm + periodic/shutdown checkpoint) + posture field + `docs/worm-audit.md` (documents the
    immutable-sink ops requirement).
  - **5b** manager scoped worker tokens (self-report routes only).
  - **5d** inbound worker-frame validation (`worker_frame_on_invalid` drop/reject).
- **Unified environment profile (task #25) — DONE:** top-level `environment: dev|production` (default
  production), `compute_security_posture()` self-report, `_validate_environment_profile` production
  assertion, startup posture log, and an operator/admin-gated `/api/security-posture`.
- **Independent re-verification (read-only, 43 agents over 26 commits):** report in
  `docs/verify-hardening-body-2026-05-31.md`. 13 confirmed / 18 refuted, plus the completeness critic
  surfaced a NEW high-severity manager priv-esc.
- **Verification remediation wave (task #29-#32) — DONE & merged.** 11 confirmed findings + 2 critic findings
  closed (table in the verify doc). Highlights:
  - **HIGH** egress SSRF was wired into only `/api/connect`; `POST /api/sessions` + `/api/profiles/{id}/connect`
    reached connectors unguarded → moved the guard into the `SessionRegistry.create_session/update_session`
    **chokepoint** so every route is covered by construction (`a38ff2e5`).
  - **HIGH** manager `POST /agent/{id}/register` merged a free-form body over the full status class → a
    worker token could inject `pending_command_*` (operator command queue) into any agent; also `config`
    (restart-spawn path) and missing `agent_id` pattern. Closed with an `_OPERATOR_FIELDS` reject-list +
    `^[\w\-]+$` pattern (`8c925c18`, `c9004c75`).
  - **MED** embedded-IPv4 IPv6 egress forms (NAT64/6to4/compat) + DNS fail-closed (`ed836e02`); `/snapshot`
    redaction bypass + browser-quota leak (`c74419f6`).
  - **LOW** IdP secret floor + empty-key HMAC (`5183c31c`); posture recon-map authz (`957baef9`); PAM relay
    egress (`44314b7a`); recording symlink/perms (`bf5f8086`).

## Detailed checklist for next session

- [x] **Resolve the stray WIP** — DONE. `transports/ws_transport.py` (+ tests) committed in `8e9840e4`/
  `b2b54e91`, `ws_session.py` (+ tests) tracked, `transports/__init__.py` lazy edit committed. The local
  full-gate coverage failure is resolved.
- [ ] **De-pollute the `test_limiter` flake** (`test_send_eviction_never_drops_the_inserting_client`) — find
  the earlier test in the random order that leaves shared RateLimiter/clock state; a reliability follow-up.
- [ ] **Deferred verification residuals (task #33)** — focused follow-ups, none remote-unauth exploits:
  - **M7** per-agent worker token = HMAC(secret, agent_id) so a worker can't impersonate another agent's
    self-report (residual is impersonation only; command-injection already closed by V-H2). Needs a
    token-distribution design decision.
  - **L9** bind the IdP response signature to the request (nonce) — currently replayable within the 300s
    `max_age`. Cross-cutting request/response protocol change.
  - **M3** DNS-rebinding: pin the resolved IP into `connector_config` (connector re-resolves at connect time).
    Documented in `egress.py`; needs connector/SNI/known-hosts plumbing.
  - **5d** narrow the per-frame `except` in `websockets_impl.py` to the builder calls only (don't mask a
    downstream broadcast/redaction failure as "invalid frame" over partially-mutated state).
- [ ] **Doc reconciliation (residual #26):** add MERGED/status banners to the older review/plan docs; refresh
  RELEASE_READINESS + a CHANGELOG section; document the new config fields (`environment`, `audit.chain_*`,
  `webhook_idp_require_signed_response`, `webhook_idp_forward_headers/cookies`) and the
  env-profile/posture *advisory-vs-enforcing* asymmetry (noted in the verify doc).
- [ ] **Docker images** still NEED-BUILD-VALIDATION (can't build locally): `docker compose -f
  docker/docker-compose.yml build` to confirm the uv-managed-venv `Dockerfile.server` (`uv sync --extra all`
  + pyte) and `Dockerfile.cf`.
- [ ] **Push to origin**: ~51 local commits on `main` are unpushed by the user's explicit choice — confirm
  before pushing.
- [ ] Optional **P2 architecture** (HA): single-active-instance vs shared control plane — design decision.
