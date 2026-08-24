# Security Policy

## Reporting a vulnerability

**Do not file a public GitHub issue for a suspected vulnerability.**

Please report vulnerabilities privately using one of these channels:

- **Preferred**: GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories) on the canonical repository — open the "Security" tab and click "Report a vulnerability". This routes the report directly to the maintainers and tracks it as a draft GHSA advisory.
- **Email**: `security@provide.io` — encrypted submissions accepted (PGP key
  forthcoming; in the interim, request a key via the same address before
  sending sensitive details).

When you report, please include:

- A description of the issue, including the impact and any conditions
  required to trigger it.
- The affected package, version, and commit SHA (if you have it).
- A proof-of-concept or reproduction steps — terminal transcripts,
  curl/wscat commands, or a small repro repo are all useful.
- Your preferred name/handle for credit in the advisory (or "anonymous").

## Response timeline

We aim to:

- **Acknowledge** every report within **72 hours**.
- **Triage and assess severity** (CVSS v3.1) within **7 days**.
- **Ship a patch or mitigation** for confirmed High/Critical issues within
  **30 days** of the initial report, and within **90 days** for Medium/Low.
- **Publish a coordinated advisory** (GHSA + CVE if applicable) at the same
  time as the patch release, crediting reporters who chose to be named.

If we believe a longer embargo is required (e.g., a coordinated multi-vendor
issue), we'll explain why when we acknowledge the report and propose a
revised disclosure date.

## Coordinated disclosure policy

Our default is **90-day coordinated disclosure**: we'll keep the report
private until either (a) a fix has shipped or (b) 90 days have elapsed from
the initial report, whichever comes first. We'll work with you on the exact
publication timing — earlier disclosure is welcome when a fix is ready
sooner; later is possible when the issue genuinely needs a longer embargo.

We won't pursue legal action against good-faith security researchers who
follow this policy.

## Supported versions

| Version line | Supported |
|---|:---:|
| `0.5.x` (current) | ✅ |
| `< 0.5` | ❌ |

The 0.x line follows semantic-minor support: only the current minor receives
security fixes. Once 1.0 ships, the policy will broaden to the current
major plus the previous minor.

## Scope

In scope:

- All code under `packages/provide-uterm*/`.
- The reference server (`uterm server`), its REST/WebSocket surface, and
  the bundled frontend.
- The MCP server (`uterm-mcp`).
- The Cloudflare Worker / Durable Object adapter.
- The `provide-uterm-platform` PTY, PAM, and LD_PRELOAD capture surfaces.

Out of scope:

- Issues that require a malicious operator who already has admin role on
  the hub.
- Bugs in third-party dependencies (please report those upstream — we'll
  bump the lock file once a fix lands).
- Best-practice deployment hardening (TLS configuration, network ACLs,
  OS-level controls) — these are the operator's responsibility, but we
  document recommendations in `docs/security-considerations.md`.

## Hardening guidance

For a comprehensive map of what's already in place and what remains, see
[`docs/security-considerations.md`](docs/security-considerations.md). The
short version: deploy behind TLS, use `--auth jwt` for production or
`--auth dev_token` for local development, run with `--require-authorized-keys`
if exposing SSH, and route recordings through an encrypted-at-rest store if
they may contain credentials.
The reference recording module writes plaintext JSONL by design (it's
the operator-friendly default for the AGPL build); enterprise
encrypted-storage hooks are a planned commercial / enterprise-tier
feature.
