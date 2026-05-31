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
  - **3c traceparent** (`20eb941f`, corrected by `869de5b4`): W3C `traceparent` injected into outbound
    webhooks/governance/IDP headers. CORRECTION: the original used a raw `opentelemetry.propagate.inject`
    (undeclared hard dep that broke downstream hub consumers without OTel); now uses
    `tracing.inject_trace_context()` built on `provide.telemetry.get_trace_context()` — OTel-optional, zero
    opentelemetry imports anywhere in provide-uterm. **Lesson: use `provide.telemetry`, never raw
    `opentelemetry`; and verify new imports are DECLARED deps, not just transitively present in the dev venv.**
  - **2b worker cap** (`b43370d0`): generous global `max_workers` cap; rejects new-over-cap with 1008,
    always allows reconnecting existing worker ids.
- **All HIGH-severity findings from the review are now closed.** Full gate green across every package
  (Python 100% coverage + TS typecheck/vitest) — last full run after 3c+2b merge.

## Detailed checklist for next session

**Tracking is in `docs/superpowers/plans/2026-05-31-ml-backlog.md` (authoritative, checkboxes ticked).**

Remaining — **4a needs a user decision** (build-infra, can't validate locally):
- [ ] **4a** Supply chain: enforce `uv.lock`. CI part (`uv sync --group dev` → `--frozen` across
  `.github/workflows/`) is verified-safe (`uv sync --frozen --dry-run` passes) but untestable without a
  CI run, and the CLAUDE.md "comment every step" rule conflicts with the file's existing uncommented
  steps. Dockerfile part (`docker/Dockerfile.server:57` `uv pip install --system` → lock-based) is a
  build-strategy change needing a Docker build to validate. CONFIRM approach with the user before editing.

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
