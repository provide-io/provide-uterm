# Release Readiness — v0.4.0

Live status of release-gate evidence for the 0.4.0 release. The full
governance policy is in [`docs/release-governance.md`](docs/release-governance.md);
this file tracks what's been *captured* and where the artifacts live.

## Branch and tag

- **Current RC branch:** `rc/0.4.0` (cut from `main` at `e0453a0`).
- **Latest tag:** `v0.4.0-rc1`.
- **Promotion to GA:** blocked on the gaps listed under [Known gaps](#known-gaps) below.

## Captured evidence

| Gate | Tool | Latest artifact | Result |
|---|---|---|---|
| Dependency vulnerability scan | `pip-audit` | `artifacts/release-governance/pip-audit.txt` | ✅ No known vulnerabilities (workspace packages skipped — not on PyPI yet) |
| SBOM (CycloneDX) | `cyclonedx-py environment` | `artifacts/release-governance/sbom.json` | ✅ 214 KB |
| Build artifacts | `uv build` | `dist/provide_uterm_workspace-0.4.0-{whl,tar.gz}` + per-package wheels | ✅ |
| Artifact signing (cosign keyless) | Sigstore | uploaded by CI (`.bundle` files) | ✅ runs in GHA via `id-token: write`; local run skips with notice |
| Quality gate (lint/type/test) | `scripts/run_pytest_gate.py -q` | n/a (output to stdout) | ✅ 3459 passed, 67 skipped, 0 failed (6m47s) |
| Test suite, multi-Python (CI) | `🧪 CI` workflow on `actions-test/main` at `da3cafb` | https://github.com/livingstaccato/provide-uterm-actions-test/actions/runs/26004817487 | ✅ |
| Release governance (CI) | `🛡️ Release Governance` workflow | green on `rc/0.4.0` push | ✅ |
| Release pipeline (CI) | `🚀 Release` workflow | fired on `v0.4.0-rc1` tag | ✅ |
| Baseline capture | `scripts/capture_rc_baseline.sh` | `artifacts/rc-baseline/` | ✅ pytest pass; lint/type tooling flags pre-existing tech debt (see Known gaps) |
| Artifact verification | `scripts/verify_package_artifacts.py` | stdout | ✅ "artifact verification passed (20 frontend files)" |
| Load profile | `scripts/load_profile.py` | `artifacts/load-profile/load-profile-*.txt` | ✅ 15/15 probes; p99 connect 23.86ms, p99 hello 5.68ms |
| Rollback drill | `scripts/rollback_drill.py` | `artifacts/rollback-drill/rollback-drill-*.json` | ✅ all 7 steps pass; reconnect 3.13ms; 0 5xx spike |
| Mutation gate (auth.py, changed-only) | `scripts/run_mutation_gate.py` | `mutants/mutmut-cicd-stats.json` | ⚠ 80.43% (148/184 killed); see Known gaps |

## Security posture additions this RC

Beyond the test gates, these landed during the v0.4.0-rc2 cycle to
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

## Known gaps

These prevent the GA promotion from `rc/0.4.0` to a final `v0.4.0` tag.

### Mutation gate kill rate on `auth.py` (87.50%, target 100%)

`scripts/run_mutation_gate.py --changed-only` triggers a full mutmut
run on `packages/provide-uterm/src/provide/uterm/auth.py` whenever any
change touches that file. The current kill rate is 87.50% (161 of 184
mutations killed); 23 mutants still survive in the internal option
parsers and `_load_entries` branches.

**Progress this RC:** `packages/provide-uterm/tests/test_auth_helpers.py`
grew from 40 to 60 tests across two batches. Cumulative move: 70.65% →
80.43% → 87.50%.

**Next pass:** use `mutmut show <id>` against the 23 remaining survivors
to identify their exact mutation patterns and write pinning tests.

### Lint / type / security tooling rc-baseline noise

The rc-baseline capture surfaces non-zero exit codes on `ruff`, `mypy`,
`ty`, and `bandit`. These are pre-existing tech-debt items, not new
regressions; they don't block the test suite or coverage gates:

- `ruff`: import-block ordering on a Cloudflare module.
- `mypy`: duplicate-module conflict between `provide-uterm` and
  `provide-uterm-server` both publishing `provide.uterm.bridge`. Needs
  one of: an `--exclude`, an explicit `__init__.py` shuffle, or
  `--explicit-package-bases` in the mypy invocation.
- `ty`: missing-attribute warnings on dynamically-attached `__uterm_*`
  metadata in `provide.uterm.ai.auth`.
- `bandit`: `nosec` annotations that don't match a real Bandit rule id;
  warning-level only.

Each item is its own targeted fix; none are release-blocking on the
test-correctness axis, but a battle-hardened GA should clear them.

### WebSocket origin validation (deployment-time)

The hub accepts WebSocket upgrade requests from any `Origin`. For
non-localhost deployments this lets a stale browser tab from any
domain probe the WS endpoint. Adding an `--allowed-origins` flag
(comma-separated allowlist; rejects mismatches at the 101 upgrade)
is straightforward and listed as the next priority item in
`docs/security-considerations.md` §4.

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
