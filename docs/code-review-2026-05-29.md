# Code Review & Architecture Analysis: provide-uterm

_Reviewed 2026-05-29 against commit `f8db61e` on `main`._

> **Status note (2026-06):** dated point-in-time snapshot, now well behind
> `main`. Notably, Finding 4's "keep retention/redaction defaults explicit"
> action has since shipped — default secret-redaction rules
> (`bridge/hub/redaction_defaults.py`) are on by default
> (`recording.redact_sensitive=True`) and a `recording.retention_s` reaper
> exists. Findings below are preserved as reviewed.

## Findings (ordered by severity)

1. **P1: Server modules are still large and multi-purpose**
   - [runtime.py](/Volumes/data/pyv/provide-uterm/packages/provide-uterm-server/src/provide/uterm/server/runtime.py)
   - [webhooks.py](/Volumes/data/pyv/provide-uterm/packages/provide-uterm-server/src/provide/uterm/server/webhooks.py)
   - [registry.py](/Volumes/data/pyv/provide-uterm/packages/provide-uterm-server/src/provide/uterm/server/registry.py)
   - Impact: raises regression risk, makes ownership unclear, and slows security review velocity.
   - Action: continue extraction into focused modules with behavior-preserving tests.

2. **P1: Release-readiness and quality claims can drift**
   - [README.md](/Volumes/data/pyv/provide-uterm/README.md)
   - [RELEASE_READINESS.md](/Volumes/data/pyv/provide-uterm/RELEASE_READINESS.md)
   - Impact: operational confusion and potential false confidence when docs and gates disagree.
   - Action: add CI check to enforce consistency between documented quality/security claims and current artifacts.

3. **P2: Mutation debt still exists in tracked auth gate**
   - [RELEASE_READINESS.md](/Volumes/data/pyv/provide-uterm/RELEASE_READINESS.md)
   - Impact: hidden logic defects in critical auth helpers can survive despite coverage.
   - Action: finish planned auth mutation kill-rate uplift and set fail threshold to 100% for scoped gate once complete.

4. **P2: Recording/security posture is strong but still operationally sensitive**
   - [security-considerations.md](/Volumes/data/pyv/provide-uterm/docs/security-considerations.md)
   - [recording.py](/Volumes/data/pyv/provide-uterm/packages/provide-uterm/src/provide/uterm/recording.py)
   - Impact: single-operator deployment lowers user-risk, but local secret/retention hygiene still matters for incident response and backups.
   - Action: keep retention/redaction defaults explicit and validated by config tests.

5. **P3: Test suite size and mutation-kill scaffolding increase maintenance cost**
   - Broadly across `packages/*/tests`
   - Impact: slower local iteration and harder signal extraction when failures happen.
   - Action: consolidate near-duplicate mutation-kill tests into parametrized patterns where possible.

## What the repo does (current architecture)

- Core product: multiplexed terminal/session control with browser/operator workflows and lease-based hijack controls.
- Multi-package monorepo:
  - `provide-uterm` (core transport/protocol/runtime primitives)
  - `provide-uterm-server` (FastAPI-hosted server and policy/auth wiring)
  - `provide-uterm-cloudflare` (Worker/DO runtime variant)
  - `provide-uterm-client`, `provide-uterm-platform`, frontend/app packages
- Security model:
  - explicit auth modes and guardrails,
  - role/capability authorization paths,
  - webhook governance with shared-secret HMAC signatures.

## Strengths

1. Strong auth and governance hardening trajectory in recent commits.
2. Very high test coverage discipline and extensive regression suite.
3. Clear separation between core protocol components and deployment surfaces.
4. Good use of defensive defaults and explicit config validation.

## Weaknesses / risks

1. Module size concentration in server top-of-stack files.
2. Documentation drift risk between readiness docs and actual CI outcomes.
3. Mutation gate incompleteness in security-critical areas.
4. High test-volume overhead (great confidence, higher maintenance/compute cost).

## Backlog (prioritized)

1. **Quality-claim consistency CI gate**
   - Parse generated artifacts and fail when README/readiness claims are stale.
2. **Finish auth mutation debt burn-down**
   - Close remaining survivors and tighten enforcement.
3. **Continue no-behavior-change module extraction**
   - Next target: split registry/runtime concerns by cohesive boundaries.
4. **Test suite maintainability pass**
   - Consolidate repetitive mutation-kill test clusters into reusable fixtures/parametrization.

## Tech debt actions already completed in this pass

1. Extracted reusable webhook-signing helpers from server webhook manager:
   - [webhook_signing.py](/Volumes/data/pyv/provide-uterm/packages/provide-uterm-server/src/provide/uterm/server/webhook_signing.py)
   - [webhooks.py](/Volumes/data/pyv/provide-uterm/packages/provide-uterm-server/src/provide/uterm/server/webhooks.py)
2. Kept import compatibility for existing callers/tests by reusing exported names from `webhooks.py`.

## Recommended next steps

1. Add and enforce claim-consistency CI.
2. Complete auth mutation closure to 100% for scoped gate.
3. Execute next structural extraction (`runtime.py` or `registry.py`) with behavior lock tests.
