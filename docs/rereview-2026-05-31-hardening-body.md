# Re-review — 63-commit hardening body (2026-05-31)

The findings are already verified and synthesized. My job is to format the final report. Let me produce it directly.

## Verdict

The 63-commit hardening body is **sound and represents a large, genuine security/reliability improvement** — it closes the entire P0/P0.5/P1 surface of the 2026-05-29 review. However, the pass introduced **two hard, CI-gated build regressions** (`Dockerfile.server`, `Dockerfile.cf`) and several **smaller residual gaps** the hardening itself created or made more glaring (events-path redaction bypass, governance-gate Principal crash, two quota/registry counter leaks, an IPv4-mapped-IPv6 SSRF bypass). The code is in good shape; the **documentation was barely reconciled** and now reads as if a completed program is still entirely open.

## Confirmed issues

Sorted by severity. "Regression?" = introduced or worsened by this hardening body (vs. pre-existing/latent).

| Sev | Issue | Location | Fix | Regression? |
|-----|-------|----------|-----|-------------|
| **H** | `Dockerfile.server` build fails: `uv sync --extra all` needs `provide-uterm-annotation`, which is never copied into the build context (CI-gated by container-scan.yml) | docker/Dockerfile.server:67 | Add `COPY packages/provide-uterm-annotation/ ...` before the `uv sync` RUN; remove stale `NEEDS-BUILD-VALIDATION` comment | **Yes** (build broken; CI fails) |
| **M** | `Dockerfile.cf` unbuildable: `uv sync --frozen` needs server+client workspace members, COPY set omits both | docker/Dockerfile.cf:46-48,54 | Add `COPY` for `provide-uterm-client/` and `provide-uterm-server/`; fix misleading line-45 comment | **Yes** (documented build command broken; not CI-gated) |
| **M** | Governance webhook gates crash (uncaught `TypeError`) when a `Principal` is in metadata; tears down WS session on every keystroke when governance enabled. Latent claim-leak if a serializer is ever added | store.py:256; ext.py:77/132/227/277 | Project Principal to allow-listed `{subject_id, roles}` at store.py:256 (do **not** add a dataclass encoder); regression test | **Yes** (governance webhook path made reachable) |
| **M** | `_redact_frame_fields` skips non-string `analysis.raw` and never redacts `snapshot.prompt_detected` → secrets reach lower-priv viewers despite active output gate | router_impl.py:58-81; websockets_impl.py:399 | Recurse into structured values (mirror `_redact_value`); add `prompt_detected` to redacted fields; tests on both broadcast + connect-time paths | **Yes** (redaction gap in new redaction pass) |
| **M** | Ring-buffer event read path (`/events`, `/events/watch`, MCP events tools) bypasses the output-gate redaction applied to the live broadcast | router_impl.py:120-146,563-570; sessions.py:378-391; ai/server_impl.py:352-358,593,644 | Redact at write time in `append_event` using server-default ruleset (events are role-agnostic); regression test | **Worsened** (broadcast-only redaction makes asymmetry glaring) |
| **M** | Per-principal browser quota counter leaks on mid-handshake disconnect → permanent self-lockout after 25 leaks | websockets_impl.py:341,380-406 vs try@410/finally@509; connection.py:363-369 | Pull `try:` up to right after `register_browser`; guard `cleanup_task.cancel()`; route-level test | **Yes** (new quota cap) |
| **M** | Tunnel worker disconnect never prunes stale hub entries → new global `max_workers` cap counts dead sessions and rejects new workers | tunnel/fastapi_routes.py:143-160; connection.py:142,248 | Add `await hub.prune_if_idle(worker_id)` in tunnel disconnect finally (mirror websockets_impl.py:317) | **Yes** (new cap converts old slow leak into availability bug) |
| **M** | Egress metadata-IP block bypassed by IPv4-mapped IPv6 (`::ffff:169.254.169.254`) via DNS-rebinding AAAA on a configured host | egress.py:55,83; webhooks.py:70-76 | Normalize `ip.ipv4_mapped` before the membership/private checks in both guards; add mapped-form test cases | **Yes** (gap in the new egress guard) |
| **M** | Memory control-plane reaper is a no-op while the memory backend soft-deletes → unbounded resume_token/session/approval growth on the **default** backend | memory/engine.py:48-50; factory_impl.py:735-737,494 | Implement real cutoff sweep in `MemoryControlPlane.reap()`; schedule reaper unconditionally; fix false comments + test | **Yes** (reaper added but inert for default backend) |
| **L** | Webhook-IDP role normalization now case-folds → `roles:["Admin"]` now grants admin (was inert). Intended/consistent with JWT+header paths | auth.py:56 (`_filter_known_roles`) consumed at :427 | Keep the case-fold; add regression test (`["Admin"]`/`["ADMIN"]`→`{admin}`); document in IDP contract | Behavior change (intended) |
| **L** | `WebhookAuthorizationProvider.resolve_browser_role` returns raw webhook role unfiltered (bounded by store.py:223 guard; inconsistent with IDP path) | authorization.py:294 (guard at store.py:223) | Filter at the boundary: `.strip().lower()` + allow-list (or reuse `_filter_known_roles`); test bogus→viewer | Pre-existing (inconsistency widened by IDP hardening) |
| **L** | Recording/log files created world-readable before `chmod(0o600)` — TOCTOU window + 0o755 recordings dir (filenames enumerable) | recording.py:138-139,150-151; session_logger.py:62-64,67-70 | Use `os.open(..., 0o600)` + `os.fdopen` (drop post-open chmod); `mkdir(mode=0o700)`; keep the 0o600 mutation-gate test | **Yes** (new chmod hardening leaves a window) |
| **L** | Webhook egress guard blocks only cloud-metadata IPs (no private/loopback toggle), unlike connector path; ships raw headers/cookies+claims | egress.py:38-56; auth.py:396-400; authorization.py:205-214 | Add `block_private`-style param + `security.block_private_webhook_targets`; minimize header/claim payloads | **Yes** (new guard, asymmetric posture) |
| **L** | `recording.py`/`discovery.py` outbound webhooks not wired into the new egress guard (parity gap; URLs operator-configured) | recording.py:53-71; discovery.py:42-48 | Add `await assert_webhook_target_allowed(self.url)` as first stmt in `_post`/`_get`/`announce` (method is `announce`, not `report_status`) | **Yes** (parity gap vs new guard) |
| **L** | `assert_webhook_target_allowed` silently allows an empty DNS resolve (connector guard raises); unreachable via real resolver | egress.py:53-56 vs :77-80 | Add explicit `if not addresses: raise EgressBlockedError(...)` to match connector guard; test via mocked empty resolve | **Yes** (foot-gun in new module) |
| **L** | `register_browser` leaks an orphaned resume token when the per-principal quota rejects (ControlPlane store has no opportunistic prune; TTL/retention-capped) | connection.py:289-306; resume.py:79-93,174-193 | Create the resume token **after** the quota gate passes (or revoke on rejection) | **Yes** (new quota cap) |
| **L** | Resume token consumed before worker_id / `_on_resume` validation → wrong-worker or callback-rejected resume burns a valid single-use token | browser_handlers.py:400-408; resume.py:104-110,223-252 | Non-destructive `get()` for the gates, `consume()` only on the success path before issuing new token; extend test | **Yes** (consume refactor) |
| **L** | MCP `_safe_id` path-injection guard raises `ValueError` instead of returning `{"success": false}` like every other MCP validator (fails closed; just an error-contract inconsistency) | ai/server_impl.py:701,719; client/hijack.py:42-49 | Add `_reject_bad_id(...) -> dict|None` mirroring `_reject_bad_pattern`; update test to assert `success is False` | **Yes** (new `_safe_id`) |
| **L** | CF `_ensure_credentials` nulls in-memory token hashes on a transiently-missing KV entry → up-to-60s false-revocation of tunnel/share/control auth for one session | cloudflare/.../runtime.py:161-174 | Drop the `else: None` block (keep last-known hashes on a miss); real revoke writes a present-but-nulled entry that still revokes; update test | **Yes** (new KV reload) |
| **L** | SQLite reaper never physically deletes leases expiring via `lease_expires_at` without explicit `clear_lease` (latent — no in-tree caller writes `cp_leases`) | sqlite/engine.py:96; lease_store.py:53-68 | Add `OR lease_expires_at < cutoff` to the cp_leases DELETE; note wall-clock requirement; test | Pre-existing (latent, embedders only) |
| **L** | Durability advert overstates SQLite persistence: `cp_session_tokens`/`cp_leases`/`cp_approvals`/`cp_sessions` reap branches inert in reference server (only resume tokens wired) | app/control_plane.py:55-69; engine.py:84-98 | Correct `durable_state`/notes to "resume-token store only"; fix log at factory_impl.py:158; add explanatory comment | Doc/advert drift (benign) |
| **L** | `uv pip install pyte` step bypasses uv.lock (unpinned, hash-unverified) — undercuts `UV_FROZEN=1` for one dep | docker/Dockerfile.server:67-68 | Add `provide-uterm[emulator]` to server `all` extra, `uv lock`, drop the side-channel install | **Yes** (hardening introduced UV_FROZEN; this dep escapes it) |
| **L** | No OTHER undeclared third-party import introduced (negative finding — clean) | auth.py:389; tracing.py; rest_helpers.py:57 | None — `httpx` was hoisted not added; `provide.telemetry` already a hard dep; `_validate_pattern_safety` is intra-package | n/a (clean) |

## Uncertain / needs-judgment

None. Every flagged finding was verified to ground truth (code reading plus end-to-end reproduction for the build failures, the egress bypass, the Principal crash, and the chmod window). The UNCERTAIN set is empty.

## Documentation drift

The code is in good shape; the docs were only partially reconciled and now mislead. All findings are doc-only.

| Doc | Claim | Reality | Fix |
|-----|-------|---------|-----|
| docs/enterprise-hardening-review-2026-05-29.md:129-258 (**H**) | All 83 findings (incl. all 15 Highs) presented with no status markers — reads as fully open | Every High and the bulk of M/L are **merged** in-tree (verified each) | Add a STATUS banner + per-row CLOSED(commit)/OPEN; mirror ml-backlog's open set (1d,1f,5a,5b,5d) or point to it as live status |
| RELEASE_READINESS.md:71-124 (**H**) | Lists WS-origin validation and pip-audit-zero-packages as open gaps; auth.py mutation 80-87% as GA blocker; "live status" | Both gaps **fixed** (WebSocketOriginMiddleware 4403 deny-all; pip-audit `--local`); doc untouched for a month | Remove fixed gaps, re-capture pip-audit/mutation evidence, add May-28-31 security additions, or mark superseded by ml-backlog |
| 2026-05-29-p0 + 2026-05-30-p05 plans (**M**) | 41+25 checkboxes all unchecked, no MERGED banner | Both plans **fully implemented** per ml-backlog + verified code | Add `> STATUS: MERGED (<range>)` banner or tick boxes; note telnet `_rx_buf` cap now merged |
| CHANGELOG.md:5 (**M**) | Newest entry `0.5.0-dev` dated 2026-04-20 | Records **none** of the May-28-31 fixes, incl. breaking ones (webhook sig scheme change, ad-hoc-observer deny default, https-only URL rejection, CF token entropy floor, max_workers) | Add a dated section under Security/Breaking covering the behavioral/wire-format changes |
| config_schema.py:91,136,172,173,206,220,438,478,493 (**M**) | 8 new operator-facing config fields | Undocumented in CLAUDE.md, example TOML (no [control_plane]/[governance]/[security] block), ARCHITECTURE.md | Add fields to scripts/uterm-server.example.toml with comments + an "Operational config" note covering security-relevant defaults |
| routes/health.py:73-86 (**M**) | New `/readyz` gate + `/api/health` now 503s during startup | Undocumented; README still references only `/api/health` + `/healthz`; changes k8s probe contract | Document probe contract: `/healthz`=liveness, `/readyz`=readiness (503 until startup), `/api/health`=gated |
| 2026-05-30-p05 plan:17 (**L**) | Telnet client `_rx_buf` cap "deferred to a follow-up" | **Merged** (telnet_transport.py:43 `_MAX_RX_BUF_BYTES`, commit 1a0f90ad) | Strike the deferral / mark MERGED; add to ml-backlog merged list |
| 2026-05-31-ml-backlog plan:15-20 (**L**) | 4a (uv.lock --frozen Docker) "merged, pending build/CI validation" | Honest but soft — given the two Docker build failures above, validation has **not** passed | Keep the pending caveat prominent until a green docker-build/CI run is captured; note the two Dockerfile COPY breaks |
| 2026-05-31-ml-backlog plan:5-6 (**L**) | "audit dedup'd the stale review doc" | Dedup happened only inside ml-backlog's own list; the review doc was **not** annotated — the two docs disagree | Reword to "supersedes the review doc's open-item view" and/or annotate the review doc |

## Recommended actions

**Fix now (blocking — broken builds + active runtime defect):**
1. **Dockerfile.server (H):** add the `provide-uterm-annotation` COPY — this is a hard CI failure on the container-scan job; every `docker/**` or server PR is currently red until fixed.
2. **Dockerfile.cf (M):** add the `provide-uterm-client` + `provide-uterm-server` COPY instructions — the documented `pywrangler dev` image cannot build.
3. **Governance Principal crash (M):** project the Principal to `{subject_id, roles}` at store.py:256 — with governance enabled today, every authenticated keystroke crashes the gate and tears down the session. Closes the latent claim-leak too.

**Fix soon (security/availability — gaps the hardening created or worsened):**
4. **Events-path redaction bypass (M):** redact at write time in `append_event`; otherwise the events REST/MCP surface is the single unredacted egress for everything the broadcast path scrubs.
5. **`_redact_frame_fields` structured-value gap (M):** recurse into `analysis.raw`/`snapshot.prompt_detected`.
6. **IPv4-mapped-IPv6 egress bypass (M):** normalize `ipv4_mapped` in both guards; closes the metadata-IP SSRF for rebinding attackers.
7. **Browser-quota counter leak (M)** and **tunnel prune-on-disconnect (M):** the two availability regressions from the new caps — fix the try/finally boundary and add `prune_if_idle`.
8. **Memory control-plane reaper (M):** implement the real sweep and schedule it unconditionally — it leaks on the default backend.

**Fix opportunistically (L — defense-in-depth, consistency, parity):** items 9-20 in the table — authz role filtering, recording-file `os.open(0o600)` + dir perms, webhook `block_private` toggle + recording/discovery egress wiring, empty-resolve guard parity, resume-token order/leak fixes, CF KV null-on-miss, MCP `_safe_id` error contract, the unpinned-pyte Dockerfile step. None are exploitable as shipped, but each removes a foot-gun the hardening either added or left asymmetric.

**Note (keep the case-fold, just lock it in):** the webhook-IDP `.lower()` change (L) is intended and consistent with the JWT/header paths — **do not revert**; add the regression test and an IDP-contract doc note.

**Documentation reconciliation (do as one pass):** add status/MERGED banners to the review doc + P0/P0.5 plans, refresh RELEASE_READINESS.md (drop the two fixed "known gaps"), add a CHANGELOG section for the May-28-31 body (flagging the breaking webhook-signature and ad-hoc-observer-default changes), and document the 8 new config fields + the `/readyz` probe contract. The 2026-05-31 ml-backlog is the one accurate status doc — point the others at it.

**Bottom line:** the hardening body is real and high-value, but it is **not green** — two Docker builds are broken (one CI-gated) and the governance webhook path crashes live sessions. Land items 1-3 before claiming the body is shippable; 4-8 close the meaningful residual security/availability gaps; the rest plus the doc pass are cleanup.
