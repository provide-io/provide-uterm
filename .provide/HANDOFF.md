# Handoff: Enterprise Hardening & Reliability Program

_Last updated: 2026-05-31. Supersedes the prior "Test-Suite Resilience Sweep" handoff (that flake fix shipped long ago)._

## Problem / request

"Perform a comprehensive code review and detailed architectural analysis … It is vital that this is
'enterprise hardened' and 'reliable.'" The review produced
`docs/enterprise-hardening-review-2026-05-29.md` (83 code-confirmed findings: 0 critical, 16 high, 36
medium, 31 low). The ask evolved into a full review → remediate → verify program executed in priority
waves (P0 → P0.5 remaining-highs → P1 resource/resilience → M/L backlog).

## Approach / reasoning (the operating contract — keep following it)

- **Per item:** TDD (test first, RED, implement, GREEN) → **independent adversarial subagent review**
  (spec + quality + security) → appropriate **gate** → **fast-forward merge** to local `main`.
- **Gating ("smarter gating"):** leaf change → that package's suite; **cross-cutting change**
  (auth, redaction, `/api/health`, frame schemas, config validation, lifespan) → **full**
  `uv run python scripts/run_all_tests.py`. NEVER trust the wrapper exit code — grep the output for
  `FAILED` / `not reached`. To exclude the flaky memray test from the full gate, use
  `--deselect "packages/provide-uterm/tests/memray/test_event_bus_stress.py::test_event_bus_stress"`
  (node-id). **Do NOT pass a global `-m`** to `run_all_tests.py` — it clobbers each suite's own marker
  selection and re-enables e2e tests (one run produced 2001 false failures).
- **Commits:** one logical unit per commit; conventional messages; **NO AI/Claude trailers**
  (subagents' harness adds `Co-Authored-By: Claude`; strip via
  `git rebase main --exec 'm=$(git log -1 --format=%B | grep -v "^Co-Authored-By: Claude"); git commit --amend -m "$m"'`
  then verify `git diff OLD HEAD` is empty before merging). **No git rollbacks/resets** (auto-commit env).
- detect-secrets pre-commit hook re-stamps `.secrets.baseline` line numbers on test edits — `git add` it and re-commit.

## Work completed (all merged to local `main`; 54 commits ahead of origin, UNPUSHED)

- **P0 security program** + **P0.5 remaining-highs** + **P1 resource/resilience** (readiness `/readyz` +
  `uterm_ready`, 10 metrics counters, buffer/event-ring/tunnel caps, per-principal browser quota,
  control-plane reaper + WAL truncate, CF `_queue_bytes` finally-release + JWKS stale-fallback).
- **M/L backlog (this session) — Clusters 1-3 + 5c, each reviewed + full-gated + merged:**
  - **C1 data-protection** (`d6e6df9`..`979dbc5`): 1a approval-expiry sweep (was the last open **HIGH**),
    1b output-redaction fails closed, 1c keystroke/command redaction before governance webhooks,
    1e webhook-IDP role allow-list, 1g IDP-failure audit.
  - **C2 resource caps** (`4ea612d`, `9f006bd`): 2a `expect_regex` ReDoS guard, 2c DeckMux selection/pin bound.
  - **C3 config hardening** (`1c7890a`, `9a5ff99`): 3a refuse `security.mode=dev` on non-loopback, 3b CF
    bearer entropy/placeholder floor.
  - **5c MCP path-injection** (`e4abc87`, `12327c8`): `_safe_id` validation in `HijackClient` (`_wp/_hp/_sp`)
    + the `fanout_send`/`session_annotate` MCP tools.
- **All HIGH-severity findings from the review are now closed.** Full gate green across every package
  (Python 100% coverage + TS typecheck/vitest) as of the last C3 run.

## Detailed checklist for next session

**Tracking is in `docs/superpowers/plans/2026-05-31-ml-backlog.md` (authoritative, checkboxes ticked).**

Remaining surgical items (user approved continuing these; no design decision needed):
- [ ] **4a** Supply chain: `uv sync --frozen` in `docker/Dockerfile.server` + CI (`.github/workflows/`).
  Obey CLAUDE.md CI rules (no inline scripts >3 lines; comment each step). Verify `uv sync --frozen`
  works locally first (lock must be consistent). No pytest gate — build-infra only.
- [ ] **3c** `traceparent` (W3C) propagation on outbound httpx (webhooks/governance/IDP/JWKS). Server. (M)
- [ ] **2b** Global worker-registration cap + route-layer 1008 reject. NOTE: workers share static
  `subject_id="worker"`, so a *per-principal* cap would wrongly limit the fleet — use a **generous global
  cap** (`bridge/hub/connection.py` `register_worker`, needs a caller-side reject path). Server. (S–M)

Items needing a design decision from the user before coding (task #19):
- [ ] **1f / 1d** IDP webhook contract: verify the IDP *response* signature inbound + minimize which
  headers/cookies are forwarded to it (the IDP legitimately needs the auth credential — decide the allow-list).
- [ ] **5a** Audit log monotonic sequence + hash-chain (which tamper-resistance scheme?). Server.
- [ ] **5b** Manager scoped tokens — split worker-self-report vs operator authority (token model?). Platform.
- [ ] **5d** Validate inbound worker frames through `AnyFrame` (drop-bad-frame vs reject-session?). Server.

Separate / operational:
- [ ] **memray flaky baseline**: `test_event_bus_stress` baseline 71997, tol 0.15 (cutoff ~82796); this
  dev machine produces ~83670 so it tips over intermittently. Re-baseline in **CI's** environment (not
  blindly to dev) OR widen tolerance — its own small change, not bundled with feature work.
- [ ] **Push to origin**: 54 local commits on `main` are unpushed by the user's explicit choice. Confirm
  before pushing.
- [ ] Optional **P2 architecture** (HA): single-active-instance enforcement vs shared control plane —
  needs a design decision.
