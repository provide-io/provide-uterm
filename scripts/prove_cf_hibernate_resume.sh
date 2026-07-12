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
  echo "=== Level B: real_cf / wrangler e2e (requires CF_E2E or wrangler_server fixture) ==="
  uv run pytest -q -m real_cf packages/provide-uterm-cloudflare/tests/test_e2e_ws.py \
    -k "resume or hello_includes_resume" --no-cov || {
      echo "Level B skipped or failed — check wrangler dev + CF credentials (see cloudflare/README)."
      exit 1
    }
else
  echo "=== Level B: skipped (pass --real-cf to attempt live e2e) ==="
fi

echo ""
echo "Proven:"
echo "  [x] DO hibernate contract: wipe memory → restore lease → getWebSockets broadcast"
echo "  [x] Attachment role recovery (not object identity)"
echo "  [x] Resume token mint/revoke/TTL + resumed hello path"
echo "  [x] Frontend 'Resumed' status flash on hello.resumed"
echo "  [ ] Live CF DO eviction (only with --real-cf + staging)"
echo "See docs/operations/cf-hibernate-resume-ux.md"
