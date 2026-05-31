# Enterprise-Hardening M/L Backlog — execution plan

> Source: `docs/enterprise-hardening-review-2026-05-29.md` (findings A–K). P0 + P0.5 (remaining highs)
> + P1 (resource/resilience) are MERGED. This plan tracks the **still-open** items, verified against
> `main` on 2026-05-31 (audit dedup'd the stale review doc). One TDD branch per cluster; independent
> adversarial review + appropriate gate + merge per item, no AI commit trailers.

## STATUS (updated 2026-05-31) — Clusters 1, 2, 3 MERGED to local `main`
- **C1 merged** (`d6e6df9`..`979dbc5`): 1a, 1b, 1c, 1e, 1g. **All remaining High findings are now closed.**
- **C2 merged** (`4ea612d`, `9f006bd`): 2a, 2c.
- **C3 merged** (`1c7890a`, `9a5ff99`): 3a, 3b.
- **5c MERGED** (`e4abc87`, `12327c8`): MCP/client path-injection guard.
- **3c MERGED** (`20eb941f`): W3C traceparent on outbound webhooks/governance/IDP.
- **2b MERGED** (`b43370d0`): global worker-registration cap (reconnect-preserving).
- **4a + uv-consistency MERGED** (`38cdf153` CI, `83d728ee` Docker), pending USER build/CI validation:
  CI `--frozen` on every `uv sync` + pty-unit lock-bypass fixed (YAML valid, lock `--frozen`-consistent);
  Dockerfile.server → multi-stage uv-managed venv (drops `--system` + `pip install uv`), Dockerfile.cf
  `UV_FROZEN=1`. NOT done: `.python-version` (would break the 4-version CI matrix — interpreter stays
  setup-python, correct for a matrix); small follow-ups: pin `uvx` reuse/twine versions, drop redundant
  `pip-audit` install (release-governance.yml:39), modernize release.yml dry-run. Source: uv audit.
- **Pending — needs a design decision**: 1f, 1d (IDP webhook contract), 5a (audit hash-chain scheme),
  5b (manager token model), 5d (inbound-frame validation strategy).
- **Separate pre-existing item**: `tests/memray/test_event_bus_stress.py` baseline is borderline-flaky on
  this dev machine (baseline 71997, tol 0.15 → cutoff ~82796; observed ~83670). Excluded by node-id from
  gates; re-baseline in CI's environment, not blindly. Flag as its own follow-up.

## Cluster 1 — Data-protection plane: fail-closed + redacted + audited (server) — MERGED
- [x] **1a (F-HIGH)** Approval expiry sweep scheduled — `_sweep_expired_approvals()` lifespan task. `d6e6df9`
- [x] **1b (F-med)** Output-policy gate fails closed — returns `default_rules()` on webhook error. `8637078`
- [x] **1c (B-med)** Keystrokes/commands redacted before governance/fanout webhook POST. `c50810d`
- [x] **1e (A-med)** Webhook-IDP roles allow-listed (`_filter_known_roles`, DRY refactor). `e2a97d9`
- [x] **1g (G-med)** Webhook-IDP failures audited (`audit_event`). `1791abe` (policy-deny site deferred — tangled multi-branch)
- [ ] DEFER **1d (B-med)** Webhook IDP forwards full header+cookie map → minimize/allow-list. NEEDS DESIGN
  (the IDP legitimately needs the auth credential; decide which headers to forward). Pair with 1f.
- [ ] DEFER **1f (F-med)** IDP/authz responses never signature-verified inbound. NEEDS DESIGN (adds config
  `webhook_idp_require_response_signature` + response-HMAC verify; contract change).

## Cluster 2 — Resource caps / DoS leftovers — MERGED (2a, 2c)
- [x] **2a (E-med)** Hijack `expect_regex` ReDoS — `compile_expect_regex` now calls `_validate_pattern_safety`. `4ea612d`
- [x] **2b (E-med)** Generous global worker cap (`max_workers=10000`) — `register_worker` rejects a NEW
  worker_id over cap with `WebSocketException(1008)` but ALWAYS allows reconnecting existing ids. MERGED `b43370d0`.
- [x] **2c (E-med)** DeckMux `selection`/`pin` shape+size bounded (validate-before-setattr; service drops). `9f006bd`

## Cluster 3 — Outbound transport / config hardening — MERGED (3a, 3b)
- [x] **3a (I-med)** Refuse `security.mode="dev"` on non-loopback bind (+ `dev_mode_acknowledged` escape). `1c7890a`
- [x] **3b (I-med)** CF bearer-token entropy/placeholder floor at config load (unconditional; CF is public). `9a5ff99`
- [x] **3c (I-med)** W3C `traceparent` injected into outbound webhooks/governance/IDP headers (via the shared
  `_build_webhook_headers` + webhooks.py `_deliver` + IDP). MERGED `20eb941f`. (CF JWKS left out of scope.)

## Cluster 4 — Supply chain / build (build+CI, no package code)
- [x] **4a (J-med)** `uv.lock` pinning enforced — CI `--frozen` everywhere (`38cdf153`) + Dockerfiles use a
  uv-managed venv pinned to the lock instead of `uv pip install --system` (`83d728ee`). MERGED; **needs a
  `docker build` + CI run to fully validate** (can't be checked by inspection). See STATUS for the
  deliberate `.python-version` omission and small follow-ups.

## Cluster 5 — Audit integrity + client/platform hardening (mixed; each its own small branch)
- [x] **5c (A-med)** MCP path-injection — `_safe_id` validation of `worker_id`/`session_id`/`hijack_id`
  (`client/hijack.py` `_wp/_hp/_sp`) + `group_id`/`session_id` in the `fanout_send`/`session_annotate`
  MCP tools (`ai/server_impl.py`). MERGED `e4abc87`, `12327c8`. (S, client)
- [ ] **5a (G-med)** Audit log no ordering/sequence/tamper-resistance. NEEDS DESIGN: monotonic seq + optional
  prev-hash chain — which scheme? (M, server)
- [ ] **5b (A-med)** Manager one-token-total-authority (`manager/auth.py:86`). NEEDS DESIGN: scoped tokens
  (self-report vs operator) — token model? (M, platform)
- [ ] **5d (K-low)** Inbound frames not validated through `AnyFrame`; one bad worker frame tears down whole
  session. NEEDS DESIGN: drop-bad-frame vs reject-session. (M, server [+frontend decode])

## Execution order: C1 → C2 → C3 (DONE) → surgical batch {5c, 4a, 3c, 2b} → design items {1f/1d, 5a, 5b, 5d}.
