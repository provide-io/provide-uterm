# Release Readiness — v0.4.0

Live status of release-gate evidence for the 0.4.0 release. The full
governance policy is in [`docs/release-governance.md`](docs/release-governance.md);
this file tracks what's been *captured* and where the artifacts live.

> **Status update — 2026-06-01 (evidence re-captured against `main`).** This v0.4.0 RC
> snapshot has been superseded by a multi-wave enterprise-hardening body plus a
> build-infra / CI overhaul on `main`. All **83** original review findings are closed
> (`docs/coverage-audit-2026-06-01.md`: 78 fixed + 5 deferred-then-merged, **0 open**),
> **all five CI workflows are green** (🧪 CI · 🔬 CodeQL · 🐋 Container Scan ·
> 🛡️ Hostile Client · 🛡️ Release Governance), and the suite is 100%-coverage across
> every package (**4846 passed / 85 skipped**, 2026-06-01 baseline). Supply-chain evidence
> (pip-audit / SBOM / `uv build`) was re-captured 2026-06-01.
>
> **Mutation perimeter — honest status (corrected this cycle).** The earlier "enforced at
> 100% on the curated perimeter" claim was an overclaim. The CI gate runs `--changed-only`
> at `killed==100`, and most perimeter files genuinely reach it — but an audit this cycle
> found three that do **not**: `auth.py` tops out at **94.02%** (11 mutants proven
> equivalent/unkillable), `registry.py` is **deferred** from the strict gate (its async
> paths — SSE heartbeat, `__init__`, background tasks — produce non-deterministic
> timeout/segfault mutants), and `routes/`, `webhooks.py`, `manager/*` are *enumerated
> without a bound mutmut suite* (a changed-only run on them currently fails, it does not
> pass). See [Known gaps](#known-gaps). Remaining non-mutation items are deferred-by-design
> (recording encryption → enterprise tier; `ty` type-drift → tracked, non-gating). The
> live-server load/rollback drills are **stale** (see table).

## Branch and tag

- **Current RC branch:** `rc/0.4.0` (cut from `main` at `e0453a0`).
- **Latest tag:** `v0.4.0-rc4` (rc1 → rc4 cut over the rc cycle).
- **Promotion to GA:** the GA-blocking security/quality gaps are now resolved (see the
  status banner above and [Known gaps](#known-gaps)); the only remaining items are
  deferred-by-design (enterprise recording-encryption) or non-gating tech debt.

## Captured evidence

| Gate | Tool | Latest artifact | Result |
|---|---|---|---|
| Dependency vulnerability scan | `pip-audit` | `artifacts/release-governance/pip-audit.txt` | ✅ No known vulnerabilities (workspace packages skipped — not on PyPI yet) |
| SBOM (CycloneDX) | `cyclonedx-py environment` | `artifacts/release-governance/sbom.json` | ✅ 211 KB (re-captured 2026-06-01) |
| Build artifacts | `uv build` | `dist/provide_uterm_workspace-0.4.0-{whl,tar.gz}` + per-package wheels | ✅ wheel + sdist re-built 2026-06-01 |
| Artifact signing (cosign keyless) | Sigstore | uploaded by CI (`.bundle` files) | ✅ runs in GHA via `id-token: write`; local run skips with notice |
| Quality gate (lint/type/test) | `scripts/run_pytest_gate.py -q` | n/a (output to stdout) | ✅ green; 2026-06-01 baseline: **4846 passed, 85 skipped** (5m37s), ruff + bandit clean. mypy/`ty` flag documented non-gating type-drift (see Known gaps) |
| Test suite, multi-Python (CI) | `🧪 CI` workflow on `actions-test/main` at `da3cafb` | https://github.com/livingstaccato/provide-uterm-actions-test/actions/runs/26004817487 | ✅ |
| Release governance (CI) | `🛡️ Release Governance` workflow | green on `rc/0.4.0` push | ✅ |
| Release pipeline (CI) | `🚀 Release` workflow | fired on `v0.4.0-rc1` tag | ✅ |
| Baseline capture | `scripts/capture_rc_baseline.sh` | `artifacts/rc-baseline/` | ✅ re-captured 2026-06-01: pytest 4846 passed, ruff + bandit rc=0; mypy/`ty` rc=1 = documented type-drift (non-gating, see Known gaps) |
| Artifact verification | `scripts/verify_package_artifacts.py` | stdout | ✅ "artifact verification passed (20 frontend files)" |
| Load profile | `scripts/load_profile.py` | `artifacts/load-profile/load-profile-*.txt` | ⚠️ **stale** — rc-cycle artifact (p99 connect 23.86ms / hello 5.68ms) retained; live re-run blocked: the script sends no auth and predates the `none`/`dev` mode removal, so WS probes 401 against all current auth modes. Follow-up: add header/token auth to the drill |
| Rollback drill | `scripts/rollback_drill.py` | `artifacts/rollback-drill/rollback-drill-*.json` | ⚠️ **stale** — rc-cycle artifact (reconnect 3.13ms, 0 5xx) retained; same root cause as load profile (`/api/*` returns 401 with no auth header). Follow-up: add header/token auth to the drill |
| Mutation gate (curated perimeter, changed-only) | `scripts/run_mutation_gate.py` | CI `mutation-gate` job (green) | ⚠️ enforced `killed==100` changed-only; **not universal** — `auth.py` = 94.02% (11 equivalents), `registry.py` deferred (async-unstable), `routes/`/`webhooks.py`/`manager/*` enumerated without a bound suite. See Known gaps |

## Security posture additions this RC

Beyond the test gates, these landed over the v0.4.0-rc2 … rc4 cycle to
harden the out-of-box posture:

- **`SECURITY.md`** — disclosure channel, 72h ack / 90d coordinated
  disclosure SLA, in-scope / out-of-scope inventory.
- **`docs/security-considerations.md`** — comprehensive non-test
  posture checklist (10 layers; current state per item).
- **`.github/dependabot.yml`** — weekly updates across pip / npm /
  github-actions / docker ecosystems, with patch+minor grouping so
  the PR queue is reviewable.
- **`.github/workflows/container-scan.yml`** — Trivy scan of the
  Dockerfile.server build on push, PR, weekly cron, and dispatch;
  HIGH/CRITICAL gate fails the run, SARIF uploaded to the Security
  tab.
- **Default secret-redaction ruleset** — `redaction_defaults.py`
  ships out-of-box rules for AWS keys, GitHub PATs, Slack tokens,
  JWTs, PEM private-key blocks, and generic password/api_key/token
  shapes. 25 parametrised tests verify each format redacts and
  that innocuous text passes through.
- **JWKS scheme preflight** — CF JWT validator now rejects
  non-http(s) JWKS URLs before opening them (turns a bandit B310
  false positive into a real defence-in-depth check).
- **MD5 hash semantics** — non-security uses now declare
  `usedforsecurity=False` so static analysis (bandit, ruff) and the
  CPython hashlib itself understand the boundary.

## Security and auth changes (latest)

- **Durability capabilities endpoint now authenticated** — `GET /api/durability/capabilities`
  is protected by the standard auth dependency; public liveness remains
  `GET /healthz`.
- **Governance webhooks now emit shared-secret auth and HMAC signatures** —
  policy, fanout, behavioral-audit, and output-policy webhooks now send
  both `X-Webhook-Secret` and `X-Uterm-Signature: sha256=<hmac>`.
- **Configured webhook IDP is now enforced in route auth path** —
  when `auth.identity_provider=webhook`, HTTP/WS auth resolution runs
  through the configured IDP instead of local-only resolution.

## Known gaps

The original GA-blocking security gaps are resolved (WS origin) or cleared
(lint / bandit tooling). Mutation enforcement was **corrected this cycle** from an
overclaim to an honest status (below) — its gaps are test-quality, not security holes
(all code stays 100% line/branch covered). What else remains is deferred-by-design
(enterprise recording-encryption) or non-gating tech debt. Current state (2026-06-01):

### Mutation gate — enforced changed-only, but NOT universal 100% (corrected 2026-06-01)

The CI `mutation-gate` job runs `--changed-only` at `--min-mutation-score 100`
(`killed==total`), so a PR that touches a perimeter file must kill **every** mutant
in it. Most perimeter files genuinely reach that. The earlier "perimeter enforces
100%" framing was an **overclaim**: a full-gate audit this cycle found three classes
of file that cannot pass a strict, deterministic `killed==100`:

- **`auth.py` — 94.02% (full gate, 173/184 killed).** The rc-cycle 87.50% snapshot is
  superseded. Two further mutants were killed this cycle (exact-message pinning); the
  remaining **11 are provably equivalent** (codec-case flips, `split` maxsplit on an
  unread tail, falsy-default swaps, etc.) and the mutmut trampoline hides the mutant
  body from source inspection, so no test can distinguish them. Documented as
  `pytest.skip("equivalent mutant — …")` per the codebase's existing precedent. Because
  the strict gate counts equivalents as not-killed, a change to `auth.py` source would
  currently fail the gate; this is a known limitation of strict `killed==100`.
- **`registry.py` — DEFERRED from `paths_to_mutate` (2026-06-01).** 100% line/branch
  covered; a dedicated mutmut suite was identified (wiring it lifts kill from ~14% with
  no bound tests to ~53%, 219/415), but the residual is dominated by async paths —
  `watch_session_events` (SSE heartbeat), `__init__` async setup, background tasks —
  whose mutants **hang (timeout)** or **crash the worker (segfault)** non-deterministically
  (run-to-run variance), so it cannot reach a stable `killed==100`. Commented out of the
  perimeter with the suite preserved as a re-enablable block; re-add once the async
  mutation-determinism work lands.
- **`routes/`, `webhooks.py`, `manager/process.py`, `manager/config.py` — enumerated
  without a bound suite.** They sit in `paths_to_mutate` as aspirational targets but have
  no dedicated tests in `tests_dir`, so a changed-only run on them currently **fails**
  (`no_tests` / survivors), it does not pass. The prior "safe to keep enumerated even
  before dedicated mutmut suites exist" comment was incorrect and has been fixed in
  `pyproject.toml`. Building these suites (or removing the files from the perimeter) is a
  tracked follow-up.

Net: the curated perimeter delivers strong, real mutation enforcement on its
synchronous, suite-backed files, but it does **not** guarantee a universal 100% — treat
the three classes above as honest, tracked gaps rather than enforced guarantees.

### Lint / type / security tooling — mostly CLEARED (2026-06-01)

- `ruff`: **CLEAR.** The full repo passes `ruff check` / `ruff format`; the residual
  import-block ordering is fixed (CI gates ruff on the core package + scripts).
- `mypy` duplicate-module (`provide.uterm.bridge` published by two packages):
  **RESOLVED.** The refactor consolidated `provide.uterm.bridge` into the core package
  only, so the collision no longer occurs. Strict mypy gates the core package green.
- `bandit`: **CLEARED.** The `# nosec BXXX — reason` form made bandit parse the reason
  *words* as test ids ("Test in comment: … is not a test name"). Reformatted to
  `# nosec BXXX  # reason` (text after a second `#` is ignored by bandit) — 0 warnings,
  suppression intact; the core bandit gate stays green.
- `ty` (Astral): **deferred-by-design, non-gating.** `ty` is informational (never fails
  CI) and currently reports ~255 accumulated type-drift findings across the workspace
  (incl. the `provide.uterm.ai.auth` dynamic-attribute warnings). Per CLAUDE.md, mypy /
  `ty` on the non-core packages are intentionally `[manual]` until the type-drift is
  cleaned as its own effort. Not a GA blocker.

### Recording encryption at rest — moved to enterprise tier

Plaintext JSONL recording stays as the open-source default. The
encrypted-at-rest implementation is now spec'd as an enterprise
module in
[`provide-terminal-monetization/docs/superpowers/specs/2026-05-18-recording-encryption-at-rest-design.md`](../provide-terminal-monetization/docs/superpowers/specs/2026-05-18-recording-encryption-at-rest-design.md).
This isn't a GA blocker for the AGPL line; it's a follow-up product.

### WebSocket origin validation (deployment-time) — RESOLVED

Shipped. `WebSocketOriginMiddleware`
(`packages/provide-uterm-server/src/provide/uterm/server/app/middleware.py`)
is always installed and closes cross-origin browser upgrades with code
4403; the `server.allowed_origins` config field
(`config_schema.py`, empty default = deny-all-cross-origin) drives the
allowlist. `docs/security-considerations.md` §4 now marks WS origin
validation as done. No longer a GA blocker.

## How to refresh evidence locally

```bash
# Full governance + SBOM + build artifacts (skips cosign without CI OIDC).
bash scripts/release_governance_check.sh

# Baseline snapshot (ruff/mypy/ty/bandit/pytest with per-tool rc codes).
bash scripts/capture_rc_baseline.sh

# Live-server-required gates (start `uterm server --config ...` first).
uv run python scripts/rollback_drill.py --base-url http://127.0.0.1:27780 \
  --session-id provide-shell --out-dir artifacts/rollback-drill
uv run python scripts/load_profile.py --base-url http://127.0.0.1:27780 \
  --worker-id provide-shell --concurrency 5 --rounds 3

# Mutation gate on changed files (auth.py during this cycle).
uv run python scripts/run_mutation_gate.py --python-version 3.13 \
  --changed-only --base-ref c8571ea --min-mutation-score 0
```
