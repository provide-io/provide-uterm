# provide-uterm Full-Codebase Re-Audit — Findings (2026-06-04)

Companion to `2026-06-04-code-review-report.md` (the 8-part architecture narrative). This document
records a **full-codebase re-audit**: every subsystem re-reviewed across four lenses (correctness,
security/concurrency, test-quality, architecture/performance), every prior "Resolved" claim
re-verified, and every material finding adversarially verified before inclusion.

**Method:** 10 read-only reviewers (8 subsystems + an auth/egress/webhooks/routes security cross-cut +
a mutation-gate-honesty cross-cut) → adversarial verification of every critical/high/medium finding →
hand re-confirmation of the two criticals, the NAT64 SSRF medium, and the regex/streaming items
(file:line below).

**Result:** the codebase is mature and well-hardened. **35/35** prior resolved claims still hold (0
regressions). One reviewer finding was refuted by verification (recorded below). Net new confirmed:
**2 critical, 4 medium, 7 low, 27 minor.**

---

## Resolved-claim verification (35/35 confirmed, 0 regressions)

Every "Resolution" + commit in the prior report was re-checked against current source. All confirmed
present with no regression. One was additionally flagged **partially_fixed**:

- **Gateway token persistence (0600/0700 perms).** Perms are applied (`_gateway.py:85,91`) but
  *after* create — a brief write-then-chmod TOCTOU window remains. Tracked as **L1** below.

All others (I/O-under-lock removal `cf9c47fa`, concurrent broadcast `707300d4`, shim removal
`fe69a008`, ReDoS guard `fd480ade`, capture umask-before-bind `b9bf8fa7`, single-syscall framing
`8bde10de`, annotation label-only fallback `a4296f46`, lease monotonic→wallclock persistence, egress
DNS-resolving guard, etc.): **confirmed_fixed**.

---

## CRITICAL

### C1 — `update_kv_session` destroys tunnel credential hashes (cloudflare)

`state/registry.py:51-72` (`update_kv_session`) builds a status dict and does a blind
`kv.put("session:{worker_id}", …)` — a full overwrite — on every worker connect and every alarm
heartbeat. The dict **omits** `worker_token_hash`, `share_token_hash`, `control_token_hash`, and
`issued_ip`. `do/session_runtime/runtime.py:169-177` (`_ensure_credentials`) re-reads the **same key**
after `_CREDENTIAL_TTL_S = 60`s; because the entry is *present* (not a transient `None`), it is treated
as authoritative and every absent field resolves to `None`. All four in-memory hashes null out → tunnel
worker / share-token / control-token auth break for the rest of the DO lifetime, re-broken every 60s.

This is precisely the false-revocation the `_ensure_credentials` docstring was written to avoid —
reintroduced through a *different writer* that doesn't round-trip the credential fields.

**Fix:** move credentials to a separate KV key (`session-creds:{worker_id}`) the status heartbeat never
touches (structurally corruption-proof), or make `update_kv_session` read-modify-write.

### C2 — `capture.c` `capture_disable()` undefined in the Linux build (platform)

`native/capture/capture.c:92` defines `capture_disable()` inside the `#ifdef __APPLE__` block but
`:240` calls it in the Linux `#else` `send_frame`. The Makefile sets `-Werror` → implicit-declaration
**compile failure on Linux**. Latent because CI's `pty-docker` native build is currently disabled.

**Fix:** move the definition above `#ifdef __APPLE__` (shared section, line 57) — it only touches the
shared global `g_capture_fd`.

---

## MEDIUM

### M1 — `resolve_approval` bare `send_text` loops abort mid-broadcast (server)

`bridge/hub/core_orchestration.py:129-130` (deny) and `:165-173` (`approval_resolved`) iterate browsers
with bare `await ws.send_text(...)`. A browser disconnecting mid-loop raises, aborting the function →
remaining browsers never receive `approval_resolved`, paused browsers stay stuck in `_paused_browsers`,
and the admin's approve/reject endpoint returns 500. The codebase's own `router_broadcast.py`
(`broadcast`, `send_hijack_state_to`) wraps every send and prunes dead sockets — this path doesn't.

**Fix:** wrap each send in `try/except Exception`, collect dead sockets, `hub.remove_dead_browsers`.

### M2 — 13 MCP tools skip `_reject_bad_id`, breaking the structured-error contract (client)

Only `fanout_send`/`session_annotate` pre-validate ids. The other 13 id-accepting tools
(`session_status/read/connect/disconnect/watch/subscribe/set_mode`, `hijack_*`, `worker_*`) let
`_safe_id`'s `ValueError` escape; FastMCP converts it to an in-band `ToolError`/`isError` instead of the
documented `{"success": false, "error": "invalid_id"}` dict. Security is intact (`_safe_id` still blocks
traversal) — this is an API-consistency bug agents will hit on any malformed id.

**Fix:** add `rejection = _reject_bad_id(<id>, "<field>"); if rejection: return rejection` to each.

### M3 — NAT64-embedded IPv4 bypasses the webhook SSRF guard (server)

`server/webhooks.py:452-464` (`_address_allowed`, used by webhook registration *and* delivery) does
**not** decode embedded-IPv4 IPv6 forms, unlike `egress.py::_check_resolved_ip`. In a NAT64 cluster,
`http://[64:ff9b::169.254.169.254]/hook` passes both checks and reaches the instance-metadata service.

**Fix:** apply `_decode_embedded_ipv4` (from `egress.py`) to any IPv6 address before the
metadata/private checks.

---

## LOW (verifier-downgraded from medium)

| # | Location | Issue → Fix |
|---|----------|-------------|
| L1 | `gateway/_gateway.py:83-91` | Token file/dir written then chmoded (TOCTOU). → atomic `os.open(...,0o600)` + umask-guarded mkdir. |
| L2 | `cloudflare/do/_webhooks.py:96-102,149-151` | Webhook `pattern` accepted with no length/ReDoS guard. → length-cap + `compile_expect_regex` at registration. Bounded by CF's 50ms CPU cap; admin-only. |
| L3 | `frontend/hijack_impl.ts:142-160` | `dispose()` doesn't clear `_approvalTimer` (self-clears at expiry). → call `_hideApprovalUI()`. |
| L4 | `platform/pty/connector.py:289-292` | `handle_input` `os.write` not OSError-guarded (asymmetry with `_read_master`; caught by retry loop). → try/except, set `_connected=False`. |
| L5 | `platform/pty/capture_connector.py:151-169` | `_forward_stdin` blocking socket in async ctx (AF_UNIX latency negligible). → executor offload / asyncio stream. |
| L6 | `annotation/_streaming.py:53` | On a match, carry reset to `""` drops the fresh-`text` tail → a second cross-boundary secret can be missed. → `self._carry = text[-self._max_carry:]` always. |
| L7 | `platform/pty/connector.py:283-284,291,326` | 4 `# pragma: no mutate` mask killable mutants (`"XXutf-8XX"`→LookupError; `>32768` off-by-one). → remove pragmas, add boundary tests, document equivalents in `mutation_equivalents.toml`. (Policy text is in `docs/mutmut-survivors-triage.md`, not `MUTATION_PATTERNS.md`.) |

---

## MINOR (27)

**core-hub:** per-send timeout for `send_hijack_state_to` (router_broadcast.py:163-188); `payloads_by_role`
per-role lock re-entry (perf, likely accept).
**gateways:** `_NoAuthServer.kbdint_auth_supported` returns True w/o challenge → kbdint silently fails
(return False); `IacNegotiator._append_sb` overflow-reset leaks subneg bytes into cleaned data
(:298-309); `TelnetWsGateway.start` binds `0.0.0.0` w/o auth guard unlike SSH (:115-130).
**cloudflare:** `_ensure_credentials` no negative-TTL backoff on KV exception; webhook secret plaintext
in SQLite (store.py:377-427 — encrypt-vs-document decision).
**frontend:** 7 duplicate `describe` blocks (hijack.test_part1.ts); module-scope mocks without cleanup
(approval-ux.test.ts); `_resolveApproval` POST missing `credentials` (hijack_impl.ts:565-578).
**ai-mcp:** `_is_internal_host` denylist bypass via trailing-dot FQDN / localhost subdomains (:46-66);
`session_subscribe` double-compiles user regex (perf).
**platform:** `PTYConnector.__init__` doesn't validate `input_mode` against `_VALID_MODES`;
`CaptureConnector` accepts but discards `input_mode`/`set_mode` always `"open"`; `poll_messages`
reaches private `_queue`; macOS `send_frame` `write()` without SIGPIPE suppression (capture.c:130).
**app-shell:** localStorage recents parsed without `Array.isArray` (ConnectPage.tsx:28); DOM listeners
never removed on unmount (HijackHost.tsx:129-161); vite proxy hardcodes `localhost:27780`.
**annotation:** `cred.aws_access_key` `AKIA[0-9A-Z]{12}` (16 chars, real keys 20) and `cred.github_token`
`…{8}` (real ≥36) too loose (_rules.py:27,37) — note: a redaction detector prefers over- to
under-matching, verify before tightening.
**security-crosscut:** verify `_resolve_jwt_key` JWKS fetch blocking claim before "fixing";
`connect_from_profile` lacks route-level egress guard (profiles.py); `/metrics` exposed without
route-level authz (api.py:32-49 — decide).
**test-quality:** dead `_is_clean()`/`_read_stats()` in `run_mutation_gate.py`; stale-allowlist warning
spam on `--changed-only` runs.

---

## Refuted by verification (recorded for transparency)

- **"No test exercises a path-injected id through the MCP surface"** — **false.**
  `provide-uterm-client/tests/ai/test_mcp_session.py:450-493` (`TestMcpPathInjection`) calls
  `fanout_send`/`session_annotate` with `../../api/keys` through the real MCP stack and asserts
  `error == "invalid_id"`. The original reviewer's grep was too narrow. (M2 is the *separate, real*
  contract gap for the other 13 tools — not a coverage gap.)
