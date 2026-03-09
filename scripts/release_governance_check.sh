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

echo "[4/4] artifact signing (cosign keyless)"
if ! command -v cosign >/dev/null 2>&1; then
  echo "cosign is not installed; signing gate cannot be completed" >&2
  exit 2
fi

# Sign each built wheel and sdist with a Sigstore keyless bundle.
for artifact in dist/*.whl dist/*.tar.gz; do
  [ -f "$artifact" ] || continue
  bundle="${OUT_DIR}/$(basename "$artifact").bundle"
  cosign sign-blob --yes "$artifact" --bundle "$bundle"
  echo "signed: $artifact -> $bundle"
done

# Sign the SBOM.
cosign sign-blob --yes "${OUT_DIR}/sbom.json" --bundle "${OUT_DIR}/sbom.json.bundle"
echo "signed: ${OUT_DIR}/sbom.json -> ${OUT_DIR}/sbom.json.bundle"

echo "release governance checks completed: ${OUT_DIR}"
