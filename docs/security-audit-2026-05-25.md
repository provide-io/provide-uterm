# Security Audit Notes - 2026-05-25

> **Superseded (2026-06-01):** This is a point-in-time snapshot. The
> post-2026-05-25 hardening body closed "Remaining Priority Work" items 2
> (hostile-client ingress tests — `.github/workflows/hostile-client.yml`)
> and 3 (default recording-redaction ruleset, on by default) and added
> further controls (sha256 WORM audit chain, connector-egress/SSRF
> chokepoint, webhook-IdP response-signature + replay protection). See
> [`docs/security-considerations.md`](security-considerations.md) for the
> current posture; the notes below are preserved as-of their date.

Scope: repository-local review for high-priority security vectors, stale
security documentation, and inexpensive hardening that can be implemented
without changing public runtime APIs.

## Verification Performed

- `rg` sweep across `.github`, `scripts`, `docs`, and server source for
  `SOURCE_DATE_EPOCH`, SBOM signing, CodeQL, Trivy, Dependabot, WebSocket
  origin validation, and disclosure policy.
- `git ls-remote https://github.com/slsa-framework/slsa-github-generator refs/tags/v2.1.0`
  to resolve the SLSA reusable workflow tag to an immutable commit.
- Offline `detect-secrets scan --no-verify` with cache/generated assets
  excluded to regenerate `.secrets.baseline`.
- `uv run bandit -r packages/provide-uterm-server/src packages/provide-uterm/src -ll -q`
  was run during triage and reported no medium-or-higher findings.

## Changes Made

- Added a `detect-secrets` pre-commit hook using `.secrets.baseline` and
  `--no-verify`, so local commits do not trigger secret-verification network
  calls.
- Regenerated `.secrets.baseline` at repo-root paths and excluded the baseline
  file itself, generated frontend bundles, and local caches.
- Pinned the SLSA reusable workflow in `.github/workflows/release.yml` to the
  commit behind `v2.1.0`:
  `f7dd8c54c2067bafc12ca7a55595d5ee9b75204a`.
- Updated `docs/security-considerations.md` to mark already-shipped controls
  accurately: `SECURITY.md`, CodeQL, Dependabot, Trivy, WebSocket origin
  validation, reproducible build timestamps, SBOM/pip-audit evidence signing,
  SLSA provenance, and SHA-pinned workflow actions.

## Current High-Priority Posture

- WebSocket cross-origin browser upgrades are blocked by default for
  cross-origin requests unless `server.allowed_origins` explicitly allows them.
- Supply-chain evidence is materially stronger than the stale checklist said:
  release governance signs artifacts, SBOM, and pip-audit output; workflows
  are SHA-pinned; CodeQL and Trivy publish SARIF to GitHub security tooling.
- Secret scanning is now enforceable in pre-commit, but the baseline is broad.
  Many entries are expected fixture/demo strings. Treat this as a gate against
  new leaks, not as proof that every historical entry has been manually
  adjudicated.

## Remaining Priority Work

1. Audit `.secrets.baseline` manually and replace fixture/demo strings with
   lower-entropy placeholders where practical.
2. Add hostile-client ingress tests: slowloris HTTP, oversized WebSocket
   frames, repeated failed WS handshakes, and webhook destination abuse.
3. Add a default recording redaction ruleset for common secrets and make the
   operator opt out explicitly.
4. Enable private vulnerability reporting/GHSA in the canonical repository
   settings; `SECURITY.md` already documents the policy.
5. Consider a CI job that runs `detect-secrets scan --no-verify` against
   tracked files and compares against `.secrets.baseline`, so pre-commit is not
   the only enforcement point.
