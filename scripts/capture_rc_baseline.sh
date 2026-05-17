#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# capture_rc_baseline.sh
#
# Snapshot the toolchain + quality-gate output of the current working tree
# under artifacts/rc-baseline/. Run before cutting an RC branch so a later
# RC reviewer can diff against a known-good baseline.
set -euo pipefail

OUT_DIR="${1:-artifacts/rc-baseline}"
mkdir -p "${OUT_DIR}"

# Toolchain provenance
uv run python --version > "${OUT_DIR}/python-version.txt" 2>&1
uv --version > "${OUT_DIR}/uv-version.txt" 2>&1
uname -a > "${OUT_DIR}/os-image.txt"
if uv run playwright --version > "${OUT_DIR}/playwright-version.txt" 2>/dev/null; then
  :
else
  echo "playwright not available" > "${OUT_DIR}/playwright-version.txt"
fi

# Dependency lock snapshot
uv lock --check
cp uv.lock "${OUT_DIR}/uv.lock"
shasum -a 256 uv.lock > "${OUT_DIR}/uv.lock.sha256"

# Mutmut-generated directory is excluded from the lint scans below — it's
# never source-of-truth code.
SRC_ROOTS=(
  packages/provide-uterm/src
  packages/provide-uterm-client/src
  packages/provide-uterm-server/src
  packages/provide-uterm-platform/src
  packages/provide-uterm-cloudflare/src
)

# Quality gates — capture full output, never abort the script on a single
# tool exiting non-zero. The pass/fail criteria are summarised at the end.
#
# mypy is run per-package because both ``provide-uterm`` and
# ``provide-uterm-server`` contribute submodules to the
# ``provide.uterm.bridge`` namespace (legacy pkgutil.extend_path pattern);
# running mypy across both source roots at once short-circuits on a
# duplicate-module error and hides every other type finding.
set +e
uv run ruff check "${SRC_ROOTS[@]}" packages/provide-uterm/tests scripts > "${OUT_DIR}/ruff.txt" 2>&1
ruff_rc=$?

: > "${OUT_DIR}/mypy.txt"
mypy_rc=0
for root in "${SRC_ROOTS[@]}"; do
  echo "##### mypy ${root} #####" >> "${OUT_DIR}/mypy.txt"
  if ! uv run mypy "${root}" >> "${OUT_DIR}/mypy.txt" 2>&1; then
    mypy_rc=1
  fi
done

uv run ty check "${SRC_ROOTS[@]}" > "${OUT_DIR}/ty.txt" 2>&1
ty_rc=$?
uv run bandit -q -r "${SRC_ROOTS[@]}" -ll > "${OUT_DIR}/bandit.txt" 2>&1
bandit_rc=$?
uv run pytest -q > "${OUT_DIR}/pytest.txt" 2>&1
pytest_rc=$?
set -e

cat > "${OUT_DIR}/pass-fail-matrix.txt" <<EOF
ruff:    rc=${ruff_rc}   (pass when 0)
mypy:    rc=${mypy_rc}   (pass when 0, last line says "Success: no issues found")
ty:      rc=${ty_rc}   (pass when 0)
bandit:  rc=${bandit_rc}   (pass when 0 at -ll severity threshold)
pytest:  rc=${pytest_rc}   (pass when 0, all collected tests pass or are explicitly skipped)
EOF

echo "Baseline captured at ${OUT_DIR}"
echo "pass/fail summary:"
cat "${OUT_DIR}/pass-fail-matrix.txt"
