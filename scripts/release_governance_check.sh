#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="${1:-artifacts/release-governance}"
mkdir -p "${OUT_DIR}"

echo "[1/4] dependency vulnerability scan"
# Use an ephemeral tool env so local preinstalls are not required.
# We intentionally do NOT pass --skip-editable: that was hiding the fact
# that all workspace packages were being skipped, which made the report
# look empty. pip-audit reports the editable + private workspace packages
# as "Dependency not found on PyPI" (expected — they aren't published yet)
# and still scans every transitive dep for CVEs.
# pip-audit emits "No known vulnerabilities found" on stderr and the skip
# table on stdout — capture both so the artifact tells the full story.
uv run --with pip-audit pip-audit --desc --local > "${OUT_DIR}/pip-audit.txt" 2>&1

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

# Cosign keyless signing needs an ambient OIDC token. GitHub Actions provides
# one via ``id-token: write`` permission (the SIGSTORE_ID_TOKEN env or the
# GHA-provided OIDC endpoint cosign auto-detects). Local runs have neither
# and would otherwise hang waiting for an interactive browser OAuth flow,
# so we skip the signing step locally with a clear notice. CI runs perform
# the real signing and upload the bundles as workflow artifacts.
COSIGN_AVAILABLE=1
if [[ -z "${GITHUB_ACTIONS:-}" && -z "${CI:-}" && -z "${SIGSTORE_ID_TOKEN:-}" ]]; then
  echo "  ↳ skipping cosign signing: no CI OIDC token detected" >&2
  echo "    (set CI=1 or SIGSTORE_ID_TOKEN=<token> to force signing locally)" >&2
  COSIGN_AVAILABLE=0
fi

if [[ "${COSIGN_AVAILABLE}" -eq 1 ]]; then
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
fi

echo "release governance checks completed: ${OUT_DIR}"
