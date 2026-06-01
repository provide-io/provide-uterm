<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Coverage Audit — original 83 findings → current status (2026-06-01)

Evidence-based cross-check of every finding in `enterprise-hardening-review-2026-05-29.md`
(83 code-confirmed: 0 critical · 16 high · 36 medium · 31 low) against the actual git history
on local `main`. Read-only audit; every cited fix was confirmed reachable, and all 16 HIGH
fixes were spot-checked in source.

## Headline

| Status | Count |
|---|---:|
| **FIXED** (commit-confirmed) | **78** |
| **Deferred-by-design → since MERGED** (1f, 1d, 5a, 5b, 5d) | **5** |
| **OPEN / UNVERIFIED** | **0** |

The HA/horizontal-scaling ceiling (an architectural limitation, not one of the 83 fix-items) is
**ACCEPTED** via ADR (`ha_safe=False` + a multi-replica startup error); the durability *advertisement*
that overstated it (G-high) was corrected. One report row (H-L root-euid PTY-with-only-a-command) the
report itself marked "uncertain — needs a human trust-model call"; not asserted as a defect.

## The 16 HIGH findings — all FIXED, each code-spot-checked

| # | Finding | Fix |
|---|---|---|
| 1 | A-H ad-hoc browser-WS fail-open | admin-only ad-hoc observers + `allow_adhoc_browser_observers` opt-out (`factory_impl.py`) |
| 2 | A-H MCP `session_create` SSRF via `url` | `urlparse(url).hostname` → `_is_internal_host` (`cc29cd61`/`e4abc87`) |
| 3 | B-H redaction only on `term` frames | recursive `_redact_value` + snapshot read-path redaction (`3301b50a`,`8ad3c723`,`c74419f6`) |
| 4 | B-H no-echo password keystrokes logged | `_at_password_prompt` → `log_send_masked` (`runtime.py`/`session_logger.py`) |
| 5 | C-H governance/IDP webhook SSRF | config-load `_require_secure_url` + runtime `assert_webhook_target_allowed` |
| 6 | C-H connector SSRF (sessions/profiles/connect) | guard moved to `SessionRegistry` chokepoint (`a38ff2e5`) |
| 7 | D-H approval resolve/reject TOCTOU | atomic `claim()` (`approvals.py`) |
| 8 | E-H telnet IAC SB unbounded | `_MAX_SB_BYTES` / `_append_sb` (`5b344b30`) |
| 9 | E-H broadcast head-of-line blocking | `_BROADCAST_SEND_TIMEOUT_S` (`router_impl.py`) |
| 10 | F-H approval expiry sweep never scheduled | `_sweep_expired_approvals` lifespan task (`d6e6df9`) |
| 11 | G-H durability advertisement overstated | `durable_state=("resume_tokens",)` (`8874e7f8`) |
| 12 | H-H PAM notify socket unauthenticated | 0o600 + SO_PEERCRED uid check (`pam_listener.py`) |
| 13 | H-H root binds socket at attacker path | capture-socket path confinement (`pam_integration.py`) |
| 14 | H-H CF token revocation not honored | `_ensure_credentials` always re-reads KV (`runtime.py`) |
| 15 | H-H CF links break after hibernation | same `_ensure_credentials` decoupling + keep-last-known (`68af4138`) |
| 16 | J-H pip-audit audits zero packages | `pip_audit --local` in CI (`ci.yml`) |

## Deferred-by-design items — all since MERGED

| Item | Was | Now |
|---|---|---|
| 1f/1d webhook-IDP response-sig + forward allow-list | NEEDS DESIGN | MERGED `5dbdbbe1`/`895c4fc2` |
| 5a WORM audit hash-chain | which scheme? | MERGED `bb96a6e4`+`266e6953`+`ef3522cf` |
| 5b manager scoped tokens | token model? | MERGED `8a15d62e`+`55e2e798` |
| 5d inbound frame validation | drop vs reject? | MERGED `b83caa78`+`28f47c0c` |
| M7 per-agent worker-token binding | token distribution | MERGED `55e2e798` |
| M3 DNS-rebinding | connector/SNI plumbing | MERGED `f5169157` (post-connect peer-IP; SNI/known_hosts preserved) |
| L9 IdP response replay binding | nonce protocol | MERGED `895c4fc2` (replay cache + optional nonce) |

## Beyond the original 83

Two later independent re-verifications drove additional fixes on top of the 83:
- `rereview-2026-05-31-hardening-body.md` → remediation waves R1–R5. Its confirmed-issues table totals
  1 H + 8 M + 14 L (the 1 H = the `Dockerfile.server` build break, since fixed); of those, ~7 M + 11 L drove
  the R1–R5 remediation work, the remainder being negative / pre-existing-latent / "clean" rows.
- `verify-hardening-body-2026-05-31.md` → 13 confirmed findings (2 HIGH: the connector-SSRF chokepoint
  gap + a manager `register` operator-command-injection priv-esc) — all remediated; see that report's
  remediation-status table.

## Still genuinely open (NOT review findings)

- **Doc reconciliation** (task #26, docs-only): this audit + the banners stamped 2026-06-01 close most of it.
- **Reliability polish** (task #34): de-pollute the `test_limiter` cross-test flake; extend the M3 peer-IP
  guard to the telnet connector (a client-package transport accessor).
- **Validation-pending**: the `uv.lock --frozen` Docker work (`38cdf153`/`83d728ee`) needs a real
  `docker build` + container-scan CI run to fully validate (cannot be checked by inspection).
