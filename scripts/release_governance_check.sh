#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="${1:-artifacts/release-governance}"
mkdir -p "${OUT_DIR}"

echo "[1/4] dependency vulnerability scan"
# Use an ephemeral tool env so local preinstalls are not required.
uv run --with pip-audit pip-audit --desc --local --skip-editable > "${OUT_DIR}/pip-audit.txt"

echo "[2/4] build artifacts"
uv build

echo "[3/4] SBOM generation"
# Use an ephemeral tool env so local preinstalls are not required.
uv run --with cyclonedx-bom cyclonedx-py environment --output-format json --output-file "${OUT_DIR}/sbom.json"

# Post-deploy manual steps (require a live server URL):
#   uv run python scripts/rollback_drill.py --base-url <URL> --session-id <ID>
#   uv run python scripts/load_profile.py --base-url <URL>
# SLSA provenance generation not yet implemented.

echo "[4/4] artifact signing (cosign keyless)"
if ! command -v cosign >/dev/null 2>&1; then
  echo "cosign binary is not installed; signing gate cannot be completed" >&2
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
