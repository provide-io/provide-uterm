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

## Work completed (all merged AND **pushed** to `origin/main` @ `b99245eb`, 2026-06-01)

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
- **Deferred residuals (task #33) — DONE & merged:** M7 per-agent worker token = HMAC(secret, agent_id),
  path-bound + enforce flag (`55e2e798`); L9 IdP-response replay protection = always-on replay cache +
  optional request-nonce (`895c4fc2`); M3 DNS-rebinding = post-connect peer-IP validation for ssh/ws
  connectors, no TLS/SSH-verification weakening (`f5169157`); 5d frame-`except` narrowed to builder calls
  (`28f47c0c`).
- **DRY session refactor (task #36) — DONE:** extracted a shared `TransportSession` base; `telnet_session`
  266→119 lines, `ws_session` committed, all three at 100% (`de587e0b`/`4a891163`). End-state with no legacy;
  `uwarp-space` (editable-symlink consumer) verified against it (43 telnet-session tests green, no changes
  needed). `ws_transport.py` adopted + hardened for websockets 16.0 (#35, `8e9840e4`).
- **Coverage audit + doc accuracy — DONE:** `docs/coverage-audit-2026-06-01.md` proves all 83 original
  findings closed (78 fixed + code-spot-checked, 5 deferred-then-merged, **0 open**); status banners stamped
  on the review/plan docs. A 16-group doc-accuracy audit found 97 inaccuracies across 36 docs — all fixed
  over 3 rounds (`427f2826` + the round-2/round-3 commits), one audit false-positive (`PatternDetector`)
  correctly rejected.
- **Full gate FULLY GREEN** (post-everything): `run_all_tests.py` → all package suites pass, **100% coverage
  every package**, 0 failures (the `test_limiter` flake did not recur). Pushed; CI running.

## Detailed checklist for next session

- [x] **Resolve the stray WIP** — DONE. `transports/ws_transport.py` (+ tests) committed in `8e9840e4`/
  `b2b54e91`, `ws_session.py` (+ tests) tracked, `transports/__init__.py` lazy edit committed. The local
  full-gate coverage failure is resolved.
- [x] **Deferred residuals (task #33)** — DONE (M7 `55e2e798`, L9 `895c4fc2`, M3 `f5169157`, 5d `28f47c0c`).
- [x] **Doc reconciliation (#26) + full doc-accuracy audit** — DONE (coverage-audit + status banners + 97
  inaccuracies fixed across 36 docs).
- [x] **Push to origin** — DONE (`b99245eb`, 2026-06-01).
- [ ] **`test_limiter` flake de-pollution + telnet M3 peer-IP guard (#34)** — the genuine remaining
  reliability follow-up. Flake: find the earlier random-order test leaving shared RateLimiter/clock state.
  Telnet M3: the ssh/ws connectors got post-connect peer-IP validation (`f5169157`); telnet needs a public
  peer-IP accessor on `TelnetTransport` (client pkg) then the same guard before first receive.
- [ ] **Docker images** — VALIDATING (2026-06-01: `docker compose build` run locally + the CI `🐋 Container
  Scan` job on the push). Confirm the uv-managed-venv `Dockerfile.server` (`uv sync --extra all` + pyte) +
  `Dockerfile.cf` build clean; watch the CI container-scan result.
- [ ] **CI watch (post-push)**: memray `test_event_bus_stress` may need a CI-env re-baseline (deselected
  locally); the `test_limiter` seed flake; the container-scan/Docker build. The scheduled Hostile-Client
  workflow was already red pre-push (independent of this work).
- [x] **`ws_session.py` / `ws_transport.py` — DONE** (#35/#36): adopted, hardened for websockets 16.0,
  refactored onto the shared `TransportSession` base, 100% covered, committed. The *file* is complete; it
  simply has no consumer yet (the WS sibling of the telnet session `uwarp` uses). Wiring it into a flow is an
  optional FUTURE step when WS is actually needed — not pending work on the module itself.
- [ ] Optional **P2 architecture** (HA): single-active-instance vs shared control plane — design decision.
