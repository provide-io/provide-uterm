# Enterprise-Hardening M/L Backlog — execution plan

> Source: `docs/enterprise-hardening-review-2026-05-29.md` (findings A–K). P0 + P0.5 (remaining highs)
> + P1 (resource/resilience) are MERGED. This plan tracks the **still-open** items, verified against
> `main` on 2026-05-31 (audit dedup'd the stale review doc). One TDD branch per cluster; independent
> adversarial review + appropriate gate + merge per item, no AI commit trailers.

## Cluster 1 — Data-protection plane: fail-closed + redacted + audited (server) — **HIGHEST VALUE**
- [ ] **1a (F-HIGH)** Approval expiry sweep never scheduled in production. `cleanup_expired()` exists
  (`bridge/hub/approvals.py:89`) but has only test callers → held commands never time out; approval store +
  hold buffers leak. **Fix:** add `_sweep_expired_approvals()` lifespan task (mirror `_sweep_expired_tunnel_tokens`
  in `app/factory_impl.py`) calling `hub.approval_store.cleanup_expired()` on interval; cancel in teardown. (S)
- [ ] **1b (F-med)** `WebhookOutputPolicyGate.get_redaction_rules` returns `[]` on webhook error/non-200
  (`ext.py:276,278`) → fail-open (no extra redaction). **Fix:** fall back to `default_rules()`
  (`redaction_defaults.py:107`) on error. FIRST trace the consumption path to confirm fallback isn't double-applied. (S)
- [ ] **1c (B-med)** `WebhookPolicyGate.intercept_input` POSTs raw keystrokes `"data": data` unredacted
  (`ext.py:70`). **Fix:** run `data` through `StreamRedactor(default_rules())` before POST. (S)
- [ ] **1d (B-med)** Webhook IDP copies full header+cookie map (Authorization bearer, session cookies) to the
  IDP payload (`auth.py` IDP `resolve_principal`). **Fix:** strip/allow-list — drop raw Authorization/Cookie. (S)
- [ ] **1e (A-med)** Webhook IDP trusts `roles`/`scopes` verbatim — no allow-list (JWT path filters to
  `{viewer,operator,admin}` at `auth.py:194-205`). MITM'd/compromised IDP mints `admin`. **Fix:** apply the
  same role allow-list filter to the webhook-IDP response. (S)
- [ ] **1g (G-med)** Governance denials & IDP failures emit only `logger.warning` — not audited. **Fix:**
  `audit_event(...)` (`audit.py:17`) for policy-deny / behavioral-kill / IDP-failure
  (`core_impl.py`, `router_impl.py`, `auth.py`). (S–M)
- DEFER **1f (F-med)** Webhook IDP/authz responses never signature-verified inbound → own branch (adds config
  `webhook_idp_require_response_signature` + response-HMAC verify; contract change). (S–M)
- Gate: server suite (`uv run pytest packages/provide-uterm-server/tests/ -q`). **Re-run full `run_all_tests.py`
  if anything touches a cross-cutting contract** (it shouldn't here — all internal).

## Cluster 2 — Resource caps / DoS leftovers (server + core)
- [ ] **2a (E-med)** Hijack `expect_regex` ReDoS — `compile_expect_regex` (`rest_helpers.py:40-54`) only
  length-caps; doesn't call `_validate_pattern_safety` (`event_bus.py:258`). **Fix:** call it. (S, server)
- [ ] **2b (E-med)** No cap on hub WORKER registrations — per-principal quota is BROWSER-only
  (`bridge/hub/connection.py:126-136`). **Fix:** per-principal worker quota mirroring the browser path. (S–M, server)
- [ ] **2c (E-med)** DeckMux `selection`/`pin` no shape/size validation (`deckmux/_presence.py:72-80`,
  `_service.py:208-216`) — `setattr` of arbitrary JSON. **Fix:** validate/clamp shape+size. (S, **CORE** — separate
  100% gate; isolate as own branch or last commit.)

## Cluster 3 — Outbound transport / config hardening (server + cloudflare)
- [ ] **3a (I-med)** `security.mode="dev"` strips HSTS/CSP/X-Frame with no loopback guard (`security.py:47`),
  asymmetric with `auth.mode=dev_token`. **Fix:** startup validator refusing dev mode on non-loopback bind. (S, server)
- [ ] **3b (I-med)** CF bearer-token validation presence-only (`cloudflare/config.py:187-189`) vs FastAPI's
  ≥32-char/placeholder floor. **Fix:** port the entropy/placeholder reject to CF config load. (S, cloudflare — root gate)
- [ ] **3c (I-med)** No `traceparent` propagation outbound (webhooks/governance/IDP/JWKS). **Fix:** inject W3C
  traceparent into outbound httpx headers. (M, server)

## Cluster 4 — Supply chain / build (build+CI, no package code)
- [ ] **4a (J-med)** `uv.lock` pinning never enforced — `Dockerfile.server:57` `uv pip install` (fresh resolve);
  CI `uv sync` no `--frozen`. **Fix:** `uv sync --frozen` in Dockerfile + CI. (S–M)

## Cluster 5 — Audit integrity + client/platform hardening (mixed; each its own small branch)
- [ ] **1f** (deferred from C1) IDP response signature verification.
- [ ] **5a (G-med)** Audit log no ordering/sequence/tamper-resistance (`audit.py:53`, non-monotonic time). **Fix:**
  monotonic seq + optional prev-hash chain. (M, server)
- [ ] **5b (A-med)** Manager one-token-total-authority (`manager/auth.py:86`). **Fix:** scoped tokens
  (self-report vs operator). (M, platform)
- [ ] **5c (A-med)** MCP path-injection — LLM `worker_id`/`session_id`/`group_id` interpolated unencoded;
  httpx resolves `../` CLIENT-side, forging requests to other routes (`client/hijack.py:169-173`). **Fix:**
  validate ids `^[\w\-]+$` (or `quote(safe="")`) before path interpolation. (S, client)
- [ ] **5d (K-low)** Inbound frames not validated through `AnyFrame`; one bad worker frame `ValidationError`
  tears down whole session (`bridge/routes/websockets_impl.py` recv). **Fix:** validate inbound + per-frame
  try/except (drop bad frame, keep session). (M, server [+frontend decode])

## Execution order: Cluster 1 → 2 → 3 → 4 → 5. Highest value: 1a, the 1b–1e fail-open/redaction set, 2a.
