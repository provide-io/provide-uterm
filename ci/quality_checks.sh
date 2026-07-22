#!/usr/bin/env bash
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Single source of truth for the static quality gate. The CI `quality` job
# (.github/workflows/ci.yml) runs this script, and developers run it locally
# to verify a branch is CI-green before pushing. It exists to close the gap
# where CI-only checks (max-LOC, SPDX headers, codegen-frames drift, event
# literals, licenses, package artifacts, performance smoke, CF vendor tree)
# were not reachable from run_all_tests.py / run_pytest_gate.py.
#
# Unlike a fail-fast sequence of separate CI steps (where the first failure
# hides every later one), this runs EVERY check and prints a per-check
# PASS/FAIL summary, then exits non-zero if any gating check failed.
#
# The two pytest suites in the CI quality job are intentionally NOT run here:
# run_all_tests.py already runs them with per-package 100% coverage. Run that
# (and run_mutation_gate.py for perimeter files) alongside this for full parity.
set -uo pipefail

# Always operate from the repository root so the relative paths below resolve
# regardless of the caller's working directory.
cd "$(dirname "$0")/.." || exit 2

failures=()

# Run a gating check; record (don't abort) on failure so the full picture shows.
step() {
  local name="$1"
  shift
  printf '\n=== %s ===\n' "$name"
  if "$@"; then
    printf '  PASS: %s\n' "$name"
  else
    printf '  FAIL: %s\n' "$name"
    failures+=("$name")
  fi
}

step "max-loc"            uv run python scripts/check_max_loc.py --max-lines 777 --baseline .ci/max-loc-baseline.json
step "codegen-frames"     uv run python scripts/codegen_frames.py --check
step "spdx-headers"       uv run python scripts/check_spdx_headers.py
step "event-literals"     uv run python scripts/check_event_literals.py
step "bare-json-ws-sends" uv run python scripts/check_bare_json_ws_sends.py
step "ruff-format"        uv run ruff format --check packages/*/src packages/*/tests scripts
step "ruff-check"         uv run ruff check packages/*/src packages/*/tests scripts
step "mypy (strict)"      ci/typecheck.sh mypy
# ty is informational: ci/typecheck.sh exits 0 for it, so it
# surfaces warnings here without gating — mirroring CI exactly.
step "ty (informational)" ci/typecheck.sh ty
step "bandit"             uv run bandit -r packages/*/src/ -ll
step "xenon"              uv run xenon --max-absolute D --max-modules D --max-average A packages/provide-uterm/src/
step "vulture"            uv run vulture packages/provide-uterm/src/ packages/provide-uterm/tests/ --ignore-names "since,password,kw,exc_type,tb,interval_s,pubkey_blob,username"
step "pip-audit"          uv run python -m pip_audit --local
step "licenses"           uv run python scripts/check_licenses.py
step "performance-smoke"  uv run python scripts/run_performance_smoke.py --iterations 100000 --enforce
step "cf-vendor-tree"     bash .ci/check_cf_vendor_tree.sh
step "package-artifacts"  uv run python scripts/verify_package_artifacts.py

printf '\n=== quality-checks summary ===\n'
if [ "${#failures[@]}" -eq 0 ]; then
  printf '  all checks passed\n'
  exit 0
fi
printf '  %d check(s) FAILED: %s\n' "${#failures[@]}" "${failures[*]}"
exit 1
