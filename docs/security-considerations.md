# Beyond the test suite: security considerations for v0.4.0

Last verified: 2026-05-25. See
[`docs/security-audit-2026-05-25.md`](security-audit-2026-05-25.md) for the
evidence-backed sweep that updated this checklist.

Tests prove behaviour is correct in expected inputs. They don't tell you
whether the **deployment posture** is correct, whether a **supply-chain
compromise** would be detected, what an **abusive client** looks like, or
how the system **fails closed**. This checklist enumerates the
non-test-suite gates worth thinking through before promoting the
``v0.4.0`` line from RC to GA.

The columns map to:

- **Layer** — what part of the stack it lives in
- **Item** — the concern
- **Current** — what's already shipping (`✅` done; `⚠` partial; `❌` not done; `—` n/a)
- **Note** — link, code path, or next step

## 1. Supply chain — what you ship

| Item | Current | Note |
|---|:---:|---|
| Dependency vulnerability scan in CI | ✅ | `🛡️ Release Governance` workflow runs `pip-audit` on every push to main/rc; transitive deps audited. |
| Dependency lock file checked in | ✅ | `uv.lock`; `uv lock --check` runs in `capture_rc_baseline.sh`. |
| SBOM (CycloneDX) on every artifact | ✅ | `artifacts/release-governance/sbom.json` regenerated each governance run. |
| Reproducible build flags | ✅ | `.github/workflows/release.yml` pins `SOURCE_DATE_EPOCH` from the commit timestamp before `uv build`. |
| Wheel signing (Sigstore keyless) | ✅ | `cosign sign-blob` in CI; bundles uploaded as workflow artifacts. Local runs skip-with-notice (no OIDC). |
| SBOM signing | ✅ | `scripts/release_governance_check.sh` signs `sbom.json` and `pip-audit.txt` with `cosign sign-blob` bundles. |
| SLSA provenance attestation | ✅ | `.github/workflows/release.yml` emits SLSA Level 3 provenance via `slsa-github-generator` and uploads `.intoto.jsonl`. |
| Lockfile poisoning / dependency confusion | ⚠ | `uv` uses PyPI by default; private packages aren't published yet. When they are, set an index priority policy in `pyproject.toml` (`tool.uv.sources`) and audit the resolver output. |
| Renovate / Dependabot weekly bumps | ✅ | Configured in `.github/dependabot.yml` for pip, npm workspaces, and GitHub Actions on a weekly cadence. |
| Typosquatting protection | ⚠ | First-time dep introduction has no review gate. Recommendation: pre-commit hook running `pip-audit --strict` against the proposed lockfile. |

## 2. Code-level static analysis

| Item | Current | Note |
|---|:---:|---|
| Ruff (E/W/F/I/N/UP/B/C4/SIM/TCH/PTH/DTZ/S/ARG/RUF) | ✅ | Clean on source tree as of `59fa664`. |
| Bandit at `-ll` (medium+) | ✅ | Two flagged issues resolved at the source in `f6e0080` (MD5 `usedforsecurity=False`, JWKS scheme preflight). |
| mypy strict | ⚠ | Clean on `provide-uterm`. ~186 pre-existing errors on `provide-uterm-server` (mostly missing `__all__` exports, untyped async wrappers, deprecated `Any` returns). Tracked in `RELEASE_READINESS.md`. |
| ty (additional type checker) | ✅ | Clean after the `setattr`/`cast` fixes in `provide.uterm.ai.auth`. |
| Mutation testing (mutmut) | ⚠ | 87.50% kill rate on `auth.py` after this RC pass (up from 70.65%). 23 survivors remain; need `mutmut show <id>` inspection. |
| Semgrep / CodeQL / Sonar | ✅ | CodeQL is wired in `.github/workflows/codeql.yml` for Python + JavaScript/TypeScript with the `security-and-quality` query pack. |
| Secret detection (detect-secrets) | ✅ | `.pre-commit-config.yaml` runs `detect-secrets-hook --baseline .secrets.baseline --no-verify`. |
| Hardcoded credential audit | ✅ | `bandit -ll` clean; no high-confidence findings. |
| `nosec` / `# noqa` annotation hygiene | ✅ | All orphan annotations resolved at the source. |

## 3. Authentication & authorization

| Item | Current | Note |
|---|:---:|---|
| Pluggable authorization (LocalProvider / WebhookProvider) | ✅ | `provide.uterm.bridge.authorization`. |
| RBAC roles (viewer / operator / admin) | ✅ | Enforced via hijack lease and webhook policy gate. |
| JWT signature validation (cloudflare) | ✅ | `provide.uterm.cloudflare.auth.jwt`; with this RC also preflight-rejects non-http(s) JWKS schemes. |
| JWT expiry + nbf enforcement | ⚠ | Confirm `pyjwt` is invoked with `options={"verify_exp": True, "verify_nbf": True}` at every call site. |
| Session timeouts on idle | ⚠ | Hijack leases time out via transfer manager. Browser WS sessions: confirm idle eviction. |
| Tunnel token rotation | ✅ | In-memory, IP-bound, time-rotated (`docs/feature-roadmap.md`). |
| Tunnel token in URL params | ⚠ | If tokens ever appear in URLs (vs Authorization header) they leak through Referer / proxy logs. Audit. |
| Password / kbd-interactive fallback policy | ✅ | `--require-authorized-keys` opt-in rejects unknown pubkeys outright. |
| Anonymous mode | ✅ | Server-side `auth.mode="dev"` and `"none"` are removed. Local development uses `dev_token`, which mints a local JWT and still exercises JWT auth. |
| Command approval workflow (dangerous-command gate) | ✅ | Buffered hold + resume implemented; webhook approval delegated. |

## 4. Transport security

| Item | Current | Note |
|---|:---:|---|
| TLS termination guidance | ⚠ | Recommend deployment-time docs: TLS terminator must enforce TLS 1.2+, no SSLv3, prefer AEAD ciphers. |
| HSTS header | ✅ | `SecurityHeadersMiddleware` emits `Strict-Transport-Security: max-age=63072000; includeSubDomains`. |
| CSP header | ✅ | `default-src 'self'; script-src 'self' cdn.jsdelivr.net; ...` |
| X-Frame-Options | ✅ | `DENY`. |
| X-Content-Type-Options | ✅ | `nosniff`. |
| Referrer-Policy | ✅ | `strict-origin-when-cross-origin`. |
| Permissions-Policy | ✅ | camera / mic / geolocation denied. |
| CORS allowlist | ⚠ | Confirm: are cross-origin browser clients in scope? If yes, document the allowlist; if no, ensure CORS is closed by default. |
| WebSocket origin validation | ✅ | `WebSocketOriginMiddleware` enforces same-origin by default and denies cross-origin browser upgrades unless explicitly allowed. |
| Subresource Integrity (SRI) on CDN assets | ✅ | `tests/test_frontend_sri.py` verifies. |
| TLS cert pinning | ❌ | Not implemented. Standard PKI is enough for most deployments; opt-in pinning could be a future feature. |

## 5. Runtime / sandbox

| Item | Current | Note |
|---|:---:|---|
| PTY isolation | ✅ | Per-session PTY; UID mapping via `provide.uterm.platform.uid_map`. |
| PAM integration for credential check | ✅ | Optional, off by default; documented in `ard-pty-architecture`. |
| Capability drop after spawn | ⚠ | Confirm `setuid`/`setgid` boundaries on the worker process before connecting the master fd. |
| seccomp profile for worker subprocess | 📋 spec'd | Open-source library spawns workers with the host's full syscall surface by design (portable across macOS/Linux/Windows, debuggable with stock tools). Enterprise-tier seccomp confinement of connector + agent subprocesses is spec'd in the `provide-terminal-monetization` repository (`docs/superpowers/specs/2026-05-19-seccomp-worker-filter-design.md`) — per-connector JSON profiles + `log`/`enforce` modes + post-exec health probe. |
| Resource limits (memory, file descriptors) | ⚠ | The server has memory baseline calibration; per-session memory caps aren't enforced via `setrlimit`. |
| LD_PRELOAD capture security | ⚠ | The platform-tier `LD_PRELOAD` integration captures stdout/stderr; verify it can't be turned into an exfil channel by a compromised worker. |
| Docker base image (Dockerfile.server) | ⚠ | Currently uses `python:3.11-slim`. Distroless or `gcr.io/distroless/python3-debian12` would shrink attack surface. |
| Container image scanning | ✅ | Trivy image scan + SARIF upload + HIGH/CRITICAL gate in `.github/workflows/container-scan.yml`. |

## 6. Recording, audit, replay

| Item | Current | Note |
|---|:---:|---|
| Session recording (JSONL) | ✅ | `provide.uterm.recording`. |
| Pattern-based annotation (anomaly detection) | ✅ | 16 built-in rules; `PatternDetector`. |
| Credential leak detection in output | ✅ | Bandit rules + redaction patterns in `redaction` module. |
| Recording PII redaction | ⚠ | Redaction is opt-in via patterns; ship a default ruleset for known secret formats (AWS keys, GitHub tokens, JWTs, etc.). |
| Recording encryption at rest | 📋 spec'd | Open-source library writes plaintext JSONL by design. Enterprise-tier encrypted-at-rest module is spec'd in the `provide-terminal-monetization` repository (`docs/superpowers/specs/2026-05-18-recording-encryption-at-rest-design.md`) — AES-GCM + KMS-backed key resolution + FIPS-mode toggle. |
| Recording retention policy | ⚠ | Local store supports `recording.retention_s` sweep; non-local stores still depend on backend-specific retention controls. |
| Tamper-evident audit log | 📋 spec'd | Open-source library writes plain audit events through `audit_event()` to whatever sink the operator configures. Enterprise-tier tamper-evidence (hash-chain + optional HMAC + optional ed25519 signing, configurable) is spec'd in the `provide-terminal-monetization` repository (`docs/superpowers/specs/2026-05-19-tamper-evident-audit-log-design.md`) — ships with an `audit-verify` CLI for post-fact verification. |
| Immutable storage hooks | ⚠ | Cloudflare DO + SQLite at the edge; on-prem reference server stores locally. |

## 7. Disclosure / response

| Item | Current | Note |
|---|:---:|---|
| `SECURITY.md` at repo root | ✅ | Present at repo root with disclosure channels, response timeline, coordinated disclosure, and scope. |
| Private vulnerability reporting via GitHub | ⚠ | Enable "Private vulnerability reporting" in the repo settings once the canonical repo is public. |
| CVE process | ⚠ | Pick a CNA (GitHub is one) and document who requests CVEs. |
| Coordinated disclosure timeline | ❌ | Common policy: 90-day max before public disclosure. Document. |
| Security advisory format | ❌ | Use `gh advisory create` or GHSA UI; commit to a CVSS score per advisory. |
| Postmortem template | ❌ | Have a postmortem template ready before the first incident, not during it. |

## 8. Operational posture

| Item | Current | Note |
|---|:---:|---|
| Rollback drill | ✅ | `scripts/rollback_drill.py` runs end-to-end; passed in `artifacts/rollback-drill/rollback-drill-20260517-230727.json`. |
| Load profile baseline | ✅ | `scripts/load_profile.py`; this RC measured p99 connect 23.86ms / p99 hello 5.68ms on the reference server. |
| SLO doc | ⚠ | Numbers exist in artifacts; codify in `docs/operations/slo.md` so on-call knows the thresholds. |
| Alerting on hijack-conflict rate | ⚠ | Metrics emit `hijack_conflicts_total`; wire to your monitoring (Prometheus alertmanager / Datadog / Grafana). |
| Rate limiting on hijack acquire | ⚠ | Confirm bucket size + refill rate in the rate limit module. |
| DoS posture | ⚠ | Browser WS hello loop is cheap (5.68ms p99); confirm no unbounded buffers in control channel processing. |
| Health endpoint | ✅ | `/api/health` returns 200 with `{"status": "ok"}`. |
| Readiness endpoint | ✅ | Distinct from health; reports `ready: true` only when sessions are loaded. |
| Graceful shutdown | ✅ | Sessions disconnect cleanly on SIGTERM (verified via rollback drill). |
| Resource exhaustion testing | ✅ | Dedicated workflow `.github/workflows/hostile-client.yml` runs burst, oversized-frame, and slow-loris probes with explicit success/failure thresholds and post-probe health checks. |

## 9. Build / CI hardening

| Item | Current | Note |
|---|:---:|---|
| GHA workflow permissions least-privilege | ✅ | Release Governance: `id-token: write` + `contents: read`. Confirm CI workflow has minimal permissions too. |
| OIDC for cloud auth | ✅ | Sigstore uses GHA OIDC; if you ever push to AWS/GCP/Azure from CI, prefer OIDC over long-lived secrets. |
| Branch protection on main / rc/** | ⚠ | Currently `actions-test` repo is a test fork; the canonical repo should require PR + green CI + signed commits on main. |
| Required reviewers on rc/** | ⚠ | Promote rc/** to be a protected branch with code-owner approval. |
| GHA workflow pinning by SHA | ✅ | Workflows pin third-party actions by full commit SHA. Dependabot keeps action updates current. |
| Restricted GitHub Actions allowlist | ⚠ | Org-level: limit which third-party actions can run. |
| Self-hosted runners | — | Not in scope; using GitHub-hosted. |

## 10. Data handling

| Item | Current | Note |
|---|:---:|---|
| Secrets in environment, not on disk | ✅ | Tunnel tokens are in-memory only. |
| Secret rotation hooks | ✅ | Token rotation per-session. |
| Encryption in transit | ⚠ | Depends on deployment-time TLS; document. |
| Encryption at rest for recordings | 📋 spec'd | Enterprise tier; see §6 and the referenced monetization spec. |
| Backups | — | Out of scope for the library; consumer concern. |
| Key management for JWT signing keys | ⚠ | If self-signing, document the key-rotation procedure. JWKS endpoint already supports it. |

## What to prioritise first

If you can only do five things before GA:

1. **Wire default redaction rules into the recording pipeline by default** (or an explicit secure mode) so secrets are not persisted in plaintext.
2. **Add signed SBOM artifacts** (cosign on SBOM) alongside existing wheel/sdist signing.
3. **Ship SLSA provenance attestations** for release artifacts.
4. **Document production branch protections** for the canonical repo (`main` and `rc/**`) and enforce them.
5. **Codify SLOs/runbooks** from existing perf/load artifacts in a dedicated operations doc.

After those, the next tier is reproducible builds (`SOURCE_DATE_EPOCH`) and SBOM signing — both shipped — and codifying the SLO doc. The two heaviest remaining items (tamper-evident audit log and worker-process seccomp confinement) have been moved to the `provide-terminal-monetization` repository as enterprise-tier specs; see the table rows above for the file paths.
