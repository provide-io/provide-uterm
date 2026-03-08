#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="${1:-artifacts/release-governance}"
mkdir -p "${OUT_DIR}"

echo "[1/4] dependency vulnerability scan"
if uv run pip-audit --help >/dev/null 2>&1; then
  uv run pip-audit --strict --desc > "${OUT_DIR}/pip-audit.txt"
else
  echo "pip-audit is not installed in this environment" >&2
  exit 2
fi

echo "[2/4] build artifacts"
uv build

echo "[3/4] SBOM generation"
if uv run cyclonedx-py --help >/dev/null 2>&1; then
  uv run cyclonedx-py environment --output-format json --output-file "${OUT_DIR}/sbom.json"
else
  echo "cyclonedx-py is not installed in this environment" >&2
  exit 2
fi

echo "[4/4] artifact signature verification precheck"
if ! command -v cosign >/dev/null 2>&1; then
  echo "cosign is not installed; signing gate cannot be completed" >&2
  exit 2
fi

echo "release governance checks completed: ${OUT_DIR}"
