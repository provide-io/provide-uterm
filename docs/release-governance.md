# Release Governance

## Release branch and tag policy

- No direct production deploy from `main`.
- Cut release candidates from `rc/<version>` branches.
- Tag RCs as `v<major>.<minor>.<patch>-rc<iteration>`.
- Promote to GA only after checklist completion and soak sign-off.

## Required controls

1. Dependency vulnerability scan passes policy (no high/critical findings).
2. SBOM generated for wheel and sdist artifacts.
3. Artifacts are signed and provenance metadata is attached.
4. Rollback drill is executed on staging and documented.

## Release checklist

1. Baseline capture complete (`scripts/capture_rc_baseline.sh`).
2. Artifact verification complete (`scripts/verify_package_artifacts.py`).
3. Supply-chain checks complete (`scripts/release_governance_check.sh`).
4. SLO/load test report attached (`scripts/load_profile.py` output).
5. On-call acknowledged current runbook and alert thresholds.
