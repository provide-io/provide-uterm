#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Prove CF hibernate + resume paths at the strongest level available locally.
#
# Level A (always, no CF account): unit contracts
# Level B (optional, wrangler/real_cf): live e2e resume
#
# Usage (from repo root):
#   bash scripts/prove_cf_hibernate_resume.sh           # unit only
#   bash scripts/prove_cf_hibernate_resume.sh --real-cf # also run real_cf e2e if env ready

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== Level A: hibernate + resume demo (CLI) ==="
uv run python scripts/demo_cf_hibernate_resume.py

echo "=== Level A: hibernate wake contract (unit) ==="
uv run pytest -q packages/provide-uterm-cloudflare/tests/test_hibernate_wake_contract.py --no-cov

echo "=== Level A: resume token + ws_routes resume (unit) ==="
uv run pytest -q packages/provide-uterm-cloudflare/tests/test_cf_resume.py --no-cov

echo "=== Level A: frontend Resumed status flash (vitest) ==="
npm run test --workspace=packages/provide-uterm-frontend -- src/session-element-resume.test.ts

if [[ "${1:-}" == "--real-cf" ]]; then
  echo "=== Level B: browser resume e2e ==="
  if [[ -n "${REAL_CF_URL:-}" && -n "${CF_E2E_JWT:-}" ]]; then
    echo "Using existing REAL_CF_URL=${REAL_CF_URL}"
    REAL_CF=1 uv run pytest -q -m real_cf packages/provide-uterm-cloudflare/tests/test_e2e_ws.py \
      -k "resume or hello_includes_resume" --no-cov
  else
    # Boots flat vendor + JWT harness + wrangler (not pywrangler — see script header).
    bash scripts/run_cf_resume_e2e_local.sh
  fi
else
  echo "=== Level B: skipped (pass --real-cf for local wrangler resume e2e) ==="
  echo "    Production: REAL_CF_URL=https://provide-uterm-cloudflare.neurotic.workers.dev"
  echo "    needs CF_E2E_JWT (+ CF Access service token if Access-gated)."
fi

echo ""
echo "Proven:"
echo "  [x] DO hibernate contract: wipe memory → restore lease → getWebSockets broadcast"
echo "  [x] Attachment role recovery (not object identity)"
echo "  [x] Resume token mint/revoke/TTL + resumed hello path"
echo "  [x] Frontend 'Resumed' status flash on hello.resumed"
echo "  [x] Level B browser resume e2e (local wrangler) when --real-cf"
echo "  [ ] Live production DO eviction under CF Access (needs prod JWT)"
echo "See docs/operations/cf-hibernate-resume-ux.md"
