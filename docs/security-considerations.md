# Beyond the test suite: security considerations for v0.4.0

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
| Reproducible build flags | ⚠ | `uv build` is deterministic given a fixed `uv.lock` but timestamps in wheels aren't pinned. Add `SOURCE_DATE_EPOCH` for byte-identical rebuilds. |
| Wheel signing (Sigstore keyless) | ✅ | `cosign sign-blob` in CI; bundles uploaded as workflow artifacts. Local runs skip-with-notice (no OIDC). |
| SBOM signing | ❌ | We sign wheels and sdist; the SBOM itself isn't signed. Easy add: `cosign sign-blob sbom.json` in the governance script. |
| SLSA provenance attestation | ❌ | Listed as "planned" in `docs/release-governance.md`. Slsa-github-generator action would emit level-3 attestation. |
| Lockfile poisoning / dependency confusion | ⚠ | `uv` uses PyPI by default; private packages aren't published yet. When they are, set an index priority policy in `pyproject.toml` (`tool.uv.sources`) and audit the resolver output. |
| Renovate / Dependabot weekly bumps | ❌ | Not configured. Without it, dep updates only land when a human notices. |
| Typosquatting protection | ⚠ | First-time dep introduction has no review gate. Recommendation: pre-commit hook running `pip-audit --strict` against the proposed lockfile. |

## 2. Code-level static analysis

| Item | Current | Note |
|---|:---:|---|
| Ruff (E/W/F/I/N/UP/B/C4/SIM/TCH/PTH/DTZ/S/ARG/RUF) | ✅ | Clean on source tree as of `59fa664`. |
| Bandit at `-ll` (medium+) | ✅ | Two flagged issues resolved at the source in `f6e0080` (MD5 `usedforsecurity=False`, JWKS scheme preflight). |
| mypy strict | ⚠ | Clean on `provide-uterm`. ~186 pre-existing errors on `provide-uterm-server` (mostly missing `__all__` exports, untyped async wrappers, deprecated `Any` returns). Tracked in `RELEASE_READINESS.md`. |
| ty (additional type checker) | ✅ | Clean after the `setattr`/`cast` fixes in `provide.uterm.ai.auth`. |
| Mutation testing (mutmut) | ⚠ | 87.50% kill rate on `auth.py` after this RC pass (up from 70.65%). 23 survivors remain; need `mutmut show <id>` inspection. |
| Semgrep / CodeQL / Sonar | ❌ | None of the deep-SAST scanners are wired up. Worth adding `github/codeql-action` for the Python tree. |
| Secret detection (detect-secrets) | ⚠ | `detect-secrets` is in dev deps but the pre-commit hook isn't enforcing on every commit. Confirm `.pre-commit-config.yaml` has the hook. |
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
| Anonymous mode | ⚠ | `--auth dev` mode for local development; ensure it can't be enabled in any production-mode config. |
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
| WebSocket origin validation | ⚠ | The hub accepts WS connections from any origin today. For non-localhost deployments, add an `--allowed-origins` flag and reject mismatches at the 101 upgrade. |
| Subresource Integrity (SRI) on CDN assets | ✅ | `tests/test_frontend_sri.py` verifies. |
| TLS cert pinning | ❌ | Not implemented. Standard PKI is enough for most deployments; opt-in pinning could be a future feature. |

## 5. Runtime / sandbox

| Item | Current | Note |
|---|:---:|---|
| PTY isolation | ✅ | Per-session PTY; UID mapping via `provide.uterm.platform.uid_map`. |
| PAM integration for credential check | ✅ | Optional, off by default; documented in `ard-pty-architecture`. |
| Capability drop after spawn | ⚠ | Confirm `setuid`/`setgid` boundaries on the worker process before connecting the master fd. |
| seccomp profile for worker subprocess | ❌ | Not implemented. Would harden against escape from a compromised shell session; consider a `seccomp.BPF` filter that allows only the syscalls the connector needs. |
| Resource limits (memory, file descriptors) | ⚠ | The server has memory baseline calibration; per-session memory caps aren't enforced via `setrlimit`. |
| LD_PRELOAD capture security | ⚠ | The platform-tier `LD_PRELOAD` integration captures stdout/stderr; verify it can't be turned into an exfil channel by a compromised worker. |
| Docker base image (Dockerfile.server) | ⚠ | Currently uses `python:3.11-slim`. Distroless or `gcr.io/distroless/python3-debian12` would shrink attack surface. |
| Container image scanning | ❌ | No Trivy/Grype scan in CI. Easy add with `aquasecurity/trivy-action`. |

## 6. Recording, audit, replay

| Item | Current | Note |
|---|:---:|---|
| Session recording (JSONL) | ✅ | `provide.uterm.recording`. |
| Pattern-based annotation (anomaly detection) | ✅ | 16 built-in rules; `PatternDetector`. |
| Credential leak detection in output | ✅ | Bandit rules + redaction patterns in `redaction` module. |
| Recording PII redaction | ⚠ | Redaction is opt-in via patterns; ship a default ruleset for known secret formats (AWS keys, GitHub tokens, JWTs, etc.). |
| Recording encryption at rest | ❌ | Recordings are unencrypted JSONL files. Encrypt-at-rest or filesystem-level encryption is a deployment concern but should be documented. |
| Recording retention policy | ⚠ | No automatic purge today; documented retention is a deployment concern. |
| Tamper-evident audit log | ❌ | Hash-chain or signed log entries would let auditors detect post-fact modification. |
| Immutable storage hooks | ⚠ | Cloudflare DO + SQLite at the edge; on-prem reference server stores locally. |

## 7. Disclosure / response

| Item | Current | Note |
|---|:---:|---|
| `SECURITY.md` at repo root | ❌ | **Most important missing item.** Should list the disclosure channel, expected response time, coordinated-disclosure timeline, and whether GHSA private advisories are accepted. |
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
| Resource exhaustion testing | ❌ | Load profile is happy-path; add a hostile-client probe (slow loris, huge frames). |

## 9. Build / CI hardening

| Item | Current | Note |
|---|:---:|---|
| GHA workflow permissions least-privilege | ✅ | Release Governance: `id-token: write` + `contents: read`. Confirm CI workflow has minimal permissions too. |
| OIDC for cloud auth | ✅ | Sigstore uses GHA OIDC; if you ever push to AWS/GCP/Azure from CI, prefer OIDC over long-lived secrets. |
| Branch protection on main / rc/** | ⚠ | Currently `actions-test` repo is a test fork; the canonical repo should require PR + green CI + signed commits on main. |
| Required reviewers on rc/** | ⚠ | Promote rc/** to be a protected branch with code-owner approval. |
| GHA workflow pinning by SHA | ⚠ | Workflows pin major versions (`@v4`, `@v8.1.0`). For supply-chain hardening, pin by full SHA (Dependabot can keep them current). |
| Restricted GitHub Actions allowlist | ⚠ | Org-level: limit which third-party actions can run. |
| Self-hosted runners | — | Not in scope; using GitHub-hosted. |

## 10. Data handling

| Item | Current | Note |
|---|:---:|---|
| Secrets in environment, not on disk | ✅ | Tunnel tokens are in-memory only. |
| Secret rotation hooks | ✅ | Token rotation per-session. |
| Encryption in transit | ⚠ | Depends on deployment-time TLS; document. |
| Encryption at rest for recordings | ❌ | See §6. |
| Backups | — | Out of scope for the library; consumer concern. |
| Key management for JWT signing keys | ⚠ | If self-signing, document the key-rotation procedure. JWKS endpoint already supports it. |

## What to prioritise first

If you can only do five things before GA:

1. **Add `SECURITY.md`** with disclosure channel + 90-day timeline. Zero-cost, highest signal.
2. **Enable Dependabot or Renovate** weekly. Closes the dependency-update gap.
3. **Strict WebSocket origin validation** with an `--allowed-origins` flag. Closes the easiest XSS-vector misuse of a stale browser tab.
4. **Default redaction ruleset** for AWS keys / GitHub tokens / JWTs in the recording pipeline. Most likely real-world incident.
5. **Container image scanning** (Trivy) on the Dockerfile.server build. Catches CVEs in the base image and OS deps before deploy.

After those, the next tier is reproducible builds (`SOURCE_DATE_EPOCH`), SBOM signing, seccomp for the worker subprocess, and codifying the SLO doc.
