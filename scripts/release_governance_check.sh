#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="${1:-artifacts/release-governance}"
mkdir -p "${OUT_DIR}"

echo "[1/4] dependency vulnerability scan"
# Audit the resolved lock, not whatever is importable at the time.
#
# This used to be `uv run --with pip-audit pip-audit --local`, which audits
# the environment it is running in -- and `--with` puts pip-audit's own
# dependency tree into exactly that environment. So a finding could name a
# package that is here only to run the audit. The lock is the honest subject:
# it is what this project resolves, it does not move when a scanner's
# requirements do, and it is the same thing on a laptop and on a runner.
#
# Workspace members are left out. They are this project rather than something
# it depends on, and they are not on PyPI for pip-audit to look up -- which is
# why the old report was padded with "Dependency not found on PyPI" lines.
# Their dependencies are still every bit as present: the export is the whole
# resolved graph minus the members themselves.
#
# pip-audit writes "No known vulnerabilities found" to stderr and its table to
# stdout, so both are captured -- the artifact is signed below and should tell
# the full story.
#
# Ignored advisories (--ignore-vuln):
# - PYSEC-2025-183 (pyjwt): disputed by upstream — "the key length is
#   chosen by the application that uses the library". Library users
#   pick the HMAC key length, so this isn't a pyjwt-side fix.
#
# A full mktemp template rather than `-t <prefix>`: GNU mktemp requires the
# trailing X's that BSD mktemp does not, so the short form runs on macOS and
# fails on the runner.
REQUIREMENTS="$(mktemp "${TMPDIR:-/tmp}/uterm-audit-requirements.XXXXXX")"
trap 'rm -f "${REQUIREMENTS}"' EXIT
uv export --no-emit-workspace --format requirements-txt > "${REQUIREMENTS}"

# The export is fully pinned, so `--no-deps` is accurate; `--disable-pip` stops
# pip-audit building a throwaway virtualenv to resolve with, which it does even
# for a pinned file and which fails where ensurepip cannot run.
uvx pip-audit --desc --no-deps --disable-pip \
    --requirement "${REQUIREMENTS}" \
    --ignore-vuln PYSEC-2025-183 \
    > "${OUT_DIR}/pip-audit.txt" 2>&1

echo "[2/4] build artifacts"
# Reproducible builds: pin every embedded timestamp to the commit time
# of the source SHA being built. The wheel/sdist will then be byte-
# identical when rebuilt from the same SHA on a different host. See
# https://reproducible-builds.org/docs/source-date-epoch/ for the
# canonical spec; hatchling, setuptools, and the standard zip/tar
# writers all honour SOURCE_DATE_EPOCH when set.
if [[ -z "${SOURCE_DATE_EPOCH:-}" ]]; then
  if commit_epoch="$(git log -1 --format=%ct 2>/dev/null)"; then
    export SOURCE_DATE_EPOCH="${commit_epoch}"
    echo "  ↳ SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH} (from git HEAD)"
  fi
fi
uv build

echo "[3/4] SBOM generation"
# Name the interpreter to describe. `uv run --with cyclonedx-bom` used to leave
# this implicit, which meant the SBOM described the ephemeral environment uv
# had just built -- cyclonedx-bom and its 30-odd dependencies included, listed
# as components of this project. `uvx` keeps the tool out of the environment,
# and the positional argument says which environment to read.
ENV_PYTHON="$(uv run --no-sync python -c 'import sys; print(sys.executable)')"
uvx --from cyclonedx-bom cyclonedx-py environment \
    --output-format json --output-file "${OUT_DIR}/sbom.json" \
    "${ENV_PYTHON}"

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

  # Sign the SBOM and the pip-audit report so the supply-chain evidence
  # itself is tamper-evident. A consumer that fetches sbom.json from a
  # release page can verify against the matching .bundle:
  #   cosign verify-blob --bundle sbom.json.bundle sbom.json
  for evidence in "${OUT_DIR}/sbom.json" "${OUT_DIR}/pip-audit.txt"; do
    [ -f "$evidence" ] || continue
    bundle="${evidence}.bundle"
    cosign sign-blob --yes "$evidence" --bundle "$bundle"
    echo "signed: $evidence -> $bundle"
  done
fi

echo "release governance checks completed: ${OUT_DIR}"
