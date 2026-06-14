# Release Governance

## Release branch and tag policy

- No direct production deploy from `main`.
- Cut release candidates from `rc/<version>` branches.
- Tag RCs as `v<major>.<minor>.<patch>-rc<iteration>`.
- Promote to GA only after checklist completion and soak sign-off.

## Required controls

1. Dependency vulnerability scan passes policy (no high/critical findings).
2. SBOM generated for wheel and sdist artifacts.
3. Artifacts are signed (cosign keyless via GHA OIDC).
4. SLSA Level 3 provenance attestation (`.intoto.jsonl`) attached
   to the GitHub Release; SBOM and pip-audit report also signed.
5. Reproducible builds — every artifact uses ``SOURCE_DATE_EPOCH``
   pinned to the source-SHA commit timestamp, so rebuilds from the
   same SHA on a different host are byte-identical. The release
   workflow sets this; the local governance script does too.
6. Rollback drill executed on staging (`scripts/rollback_drill.py`;
   artifact in `artifacts/rollback-drill/`).
7. Load profile + baseline captured (`scripts/load_profile.py` and
   `scripts/capture_rc_baseline.sh`; artifacts under `artifacts/`).

## Release checklist

1. Baseline capture complete (`scripts/capture_rc_baseline.sh`).
2. Artifact verification complete (`scripts/verify_package_artifacts.py`), including wheel/sdist metadata, package data, frontend assets for `provide-uterm-server`, import roots, and console entry points for every published Python package.
3. Supply-chain checks complete (`scripts/release_governance_check.sh`).
4. SLO/load test report attached (`scripts/load_profile.py` output).
5. On-call acknowledged current runbook and alert thresholds.
