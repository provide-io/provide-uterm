<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Enterprise Hardening & Reliability Review — provide-uterm

> **STATUS (2026-06-01): ALL 83 findings closed.** 78 fixed (commit-confirmed; all 16 HIGH spot-checked in
> code) + 5 deferred-by-design (1f/1d, 5a, 5b, 5d) since merged + **0 open**. The HA/horizontal-scaling
> ceiling is an architectural limitation ACCEPTED via ADR (`ha_safe=False` + multi-replica startup error),
> with the overstated durability advertisement corrected. Full finding→commit matrix:
> `docs/coverage-audit-2026-06-01.md`. Two later independent re-verifications drove additional fixes on top
> of these 83 — see `docs/rereview-2026-05-31-hardening-body.md` and
> `docs/verify-hardening-body-2026-05-31.md`.

_Reviewed 2026-05-29 against `main` @ `63854f5`. Scope: all eight `packages/provide-uterm*`
(~49k LOC Python + ~13.5k LOC TypeScript), CI/build, and deployment posture._

## How this was produced (methodology)

This is not a single-pass skim. The codebase was decomposed into **14 security/reliability-critical
subsystems** plus **6 system-wide lenses** (concurrency, resource/DoS, failure-resilience,
architecture/scalability, observability, supply-chain), each given to an independent deep-review agent.
**Every individual finding was then adversarially re-verified** by a separate agent whose job was to
*refute* it by reading the actual code — checking for mitigating guards, confirming the file:line, and
re-grading severity. A completeness critic then identified under-covered areas, and a **supplementary
pass** re-audited the four surfaces the first pass missed (the process-spawning manager tier, MCP/AI auth,
the SQLite durable control-plane, and DeckMux presence). In total ~136 agents ran; **93 + 18 = 111 raw
findings were filtered to 83 code-confirmed** (the rest refuted or marked uncertain). The high-severity
findings below were additionally spot-checked by hand against the source.

| | Confirmed | Refuted | Uncertain |
|---|---:|---:|---:|
| Main pass (20 reviewers) | 68 | 18 | 7 |
| Gap-fill pass (4 reviewers) | 15 | 2 | 1 |
| **Total** | **83** | **20** | **8** |

Confirmed severity mix (post-verification re-grade): **0 critical · 16 high · 36 medium · 31 low.**
(Note: only 15 findings carry an explicit `**H**` table row; the 16th HIGH is the broadcast head-of-line / per-send-timeout finding, which appears below as the ungraded `—` "Independently noted" lease-HOL row in section E rather than as its own `**H**` row. It is split out as HIGH row 9 in `docs/coverage-audit-2026-06-01.md`. Both fixes — the telnet cap and `_BROADCAST_SEND_TIMEOUT_S` in `router_impl.py` — are present in code.)

---

## Executive verdict

**provide-uterm is an unusually well-engineered, security-*conscious* codebase — but it is not yet
"enterprise hardened," and on its current architecture it is explicitly *not* horizontally reliable.**

The evidence of conscientious engineering is everywhere: constant-time token comparisons, BLAKE2b-hashed
tunnel tokens, a control-channel parser with depth/size/buffer caps and zero recursion, a webhook *delivery*
path with DNS-rebinding-resistant SSRF validation, CSP/HSTS/origin middleware, alg-confusion rejection,
placeholder-secret rejection, 100% branch coverage, and mutation testing on a security perimeter. The
verification pass *refuted 20 plausible-sounding findings* precisely because real guards already existed.
This is a high floor.

The gap between "well-built" and "enterprise hardened" is concentrated in **five themes**, none of which is
an unauthenticated remote RCE (hence **0 critical**), but several of which are serious for a multi-tenant or
regulated deployment:

1. **The hardening is applied *unevenly*.** The same control is rigorous on one path and absent on a
   sibling path. SSRF validation guards *delivery* webhooks but **not** governance/IDP webhooks **nor**
   the ssh/telnet/ws connectors **nor** the MCP `url` argument. Output redaction runs on `term` frames but
   **not** on `snapshot`/`analysis` frames. `prepare_keystrokes()` sanitizes single-session sends but
   **not** `fanout_send`. Role/entropy checks on bearer tokens exist on the FastAPI backend but **not** the
   (more exposed) Cloudflare worker. **Pattern, not one-off.**

2. **Several governance/data-protection paths fail *open*.** The behavioral-audit and output-policy gates
   `allow`/`return []` on webhook error; webhook responses that mint identity/roles are trusted with no
   signature verification and no role allow-list. The security-correct default for a *data-protection*
   control is fail-closed.

3. **Resource bounds are per-frame, not cumulative.** Many buffers (hold/input line buffers, telnet IAC
   subnegotiation ×2, tunnel-worker WS, the 2000-entry event ring, the soft-delete-only control-plane
   tables) cap a single message but grow without an aggregate ceiling — a family of authenticated-client
   memory-exhaustion vectors.

4. **No horizontal scalability, and the durability claims overstate reality.** All live arbitration state
   (leases, presence, approvals, rate-limit buckets, the WebSocket handles themselves) is per-process. Two
   replicas behind a normal load balancer **silently break correctness** (split-brain leases, invisible
   sessions). The team *knows* this (there's an ADR and an `ha_safe=False` flag) — but the
   `/api/durability/capabilities` endpoint and a startup log **advertise that approvals and leases survive
   restart when they do not** (only resume tokens are actually persisted).

5. **Two CI/operability signals are inert.** The per-PR `pip-audit` gate audits **zero** packages and is
   always green; the highest-value load/attack metrics (rate-limit drops, webhook failures, event-bus
   drops) are logged but never counted, so on-call cannot alert on them.

**Bottom line for the two adjectives in the request:**
- **"Reliable"** — single-node: yes, genuinely (graceful shutdown, rollback drill, bounded hot paths).
  Multi-node / HA: **no** — and the capabilities endpoint currently misrepresents this. Fix the
  fail-open paths, the durability advertisement, and the unbounded buffers before claiming it.
- **"Enterprise hardened"** — close, but not until the SSRF/redaction/fail-open *asymmetries* are closed,
  the manager tier gets least-privilege, recordings stop capturing no-echo passwords in plaintext, and the
  audit trail covers deny/governance-failure events. These are mostly **small, surgical fixes** (add a
  validator, move a check above an early-return, route one more field through an existing helper) — the
  architecture is sound; the gaps are the missing 5%.

---

## Architecture assessment

### What's strong
- **The inline control-channel** (`control_channel.py`) is the crown jewel: DLE/STX framing with explicit
  1 MB payload cap, 10 MB buffer cap, depth-32 limit walked *iteratively* (no recursion DoS), UTF-8
  byte-length accounting, and identical Python/TS semantics. Genuinely hardened.
- **Hub service composition** (9 services, refactor #16) is cohesive — the large `*_impl.py` files are
  mostly thin back-compat delegation, not god-objects. Lock discipline on the lease state machine is
  consistent (one shared `asyncio.Lock`, re-checked across release boundaries).
- **Defense-in-depth where it was applied**: tunnel tokens are hash-only + constant-time, one-time invites
  replace token-in-URL with HttpOnly cookies, JWT alg-confusion is rejected at startup, the CF backend
  forces `AUTH_MODE=jwt` (no dev bypass at the edge), and the frontend renders all peer-controlled strings
  via `textContent`/escaped helpers — the XSS review found **no** `innerHTML` sink taking untrusted data.
- **Honest about its own limits**: the FastAPI factory emits an ERROR when a multi-replica orchestrator is
  detected, and ships an ADR (`docs/ard-fastapi-durability-posture.md`) accepting the single-node posture.

### The scalability ceiling (the defining enterprise constraint)
`TermHub` keeps **all** authoritative live state in process memory keyed by `worker_id`, and the WebSocket
fan-out is intrinsically tied to the process that holds the socket. Concretely (`core_impl.py:588,627-636`,
`registry.py:43`, `lease.py:230-266`):
- A browser landing on replica A **cannot** see/arbitrate/send-input to a worker whose socket is on replica B.
- Two operators on different replicas can each acquire a hijack lease for the **same** `worker_id` — the
  lease check only sees its own process's state. The viewer/operator/admin arbitration guarantee is void
  across replicas.
- The only durable backend (SQLite) is a **single shared connection behind one global lock** issuing
  `BEGIN IMMEDIATE` for *every* transaction including reads (`engine.py:40,62-64`), so it serializes the
  whole control plane and lives on one node's filesystem — a per-node throughput SPOF, not a scale-out path.

**This is the single most important enterprise finding.** Recommendation: make the single-active-instance
constraint *enforced* (refuse to start under a multi-replica orchestrator unless `worker_id` sticky routing
is asserted), or invest in a shared control plane + cross-instance message bus before promising HA.

### Dual-backend divergence risk
The FastAPI hub and the Cloudflare DO independently re-implement the security-critical lease/auth state
machines with only HTTP-shape conformance tests between them. A security fix to one (e.g. the DO
token-revocation bug below) will not automatically protect the other. This is a standing maintenance hazard.

---

## Findings by theme

Severity shown is the **post-verification** grade. `H`/`M`/`L` = high/medium/low.

### A. Authentication & authorization

| Sev | Finding | Location |
|---|---|---|
| **H** | **Browser-WS authz fail-open for ad-hoc workers.** `_resolve_browser_role` skips `can_read_session` when no `SessionDefinition` is registered (worker connected ad-hoc via `/ws/worker/...`), so any authenticated viewer who knows/guesses a `worker_id` (`^[\w\-]+$`) receives that session's full output stream. The registered-session path is correctly gated; this is a fail-open for the unregistered class. | `app/factory_impl.py:414-424` |
| **H** | **MCP `session_create` SSRF via `url`.** The host denylist (`_is_internal_host`) checks the `host` arg but **not** the host *inside* `url`; the ws/ssh/telnet connector then connects to it. `session_create(connector_type="ws", url="ws://169.254.169.254/...")` reaches cloud metadata / loopback / RFC1918. | `ai/server_impl.py:215-229` → `connectors/websocket.py:88` |
| **M** | **Webhook IDP trusts roles/scopes verbatim.** `resolve_principal` builds `Principal(roles=frozenset(data.get("roles",...)))` with no allow-list (the JWT path filters to `{viewer,operator,admin}`). A compromised/MITM'd IDP webhook mints `admin`. | `auth.py:389-395` |
| **M** | **Manager: one token = total authority.** A single `hmac.compare_digest` gate; every route (spawn, kill-all, delete, restart) is then reachable. Worker agents that only self-report must hold the same omnipotent token → one leaked worker token = fleet-wide command-and-control. No role/scope separation. | `manager/auth.py:86`, `manager/routes/models.py:20` |
| **M** | **MCP path-injection / endpoint forgery.** LLM-controlled `worker_id`/`session_id`/`group_id` are interpolated unencoded into request paths; httpx resolves `../`, so `worker_id="../../api/keys"` forges a request to a *different* server route, escaping the per-tool MCP authz model. | `client/hijack.py:169-173,319,396` |
| **L** | Manager auth silently skipped when the app is wired without `config` (embedder path) regardless of bind host. | `manager/auth.py:145-152` |
| **L** | DeckMux `control_request`/`queued_input` not role-gated — a viewer can seize the collaborative owner badge and seed the keystroke-replay queue. | `websockets_impl.py:448-453`, `deckmux/_service.py:240-247` |
| **L** | Operational metrics endpoints require only authentication, not operator/admin scope — viewers read fleet telemetry. | `app/routes_wiring.py:52` |
| **L** | Per-request MCP auth is dead code; all requests run as one static principal (acceptable for stdio, but the docstrings imply a model that doesn't exist). | `ai/server_impl.py` |

### B. Secrets, data leakage & redaction

| Sev | Finding | Location |
|---|---|---|
| **H** | **Output redaction only runs on `term` frames.** `snapshot` (full screen + raw_tail) and `analysis` frames bypass `StreamRedactor`; the initial snapshot on connect bypasses `broadcast()` entirely and `last_snapshot` is stored unredacted. A secret scrubbed from the live stream leaks verbatim to every viewer (incl. non-admin) via the snapshot. | `router_impl.py:120-132`, `websockets_impl.py:390-391`, `connection.py:219` |
| **H** | **No-echo password keystrokes recorded in plaintext.** `_log_send` always calls regex-only `log_send`; char-by-char no-echo input never matches `key=value` patterns. The detector *knows* the prompt is a password (`PROMPT_ECHO_OFF`) but `log_send_masked` is referenced only in tests. | `runtime.py:280-286`, `session_logger.py:169-185` |
| **M** | **Raw keystrokes + full auth headers/cookies shipped to governance/IDP webhooks unredacted.** `WebhookPolicyGate` POSTs the raw keystroke stream; `WebhookIdentityProvider` copies the entire header+cookie map (Authorization bearer, session cookies) to the IDP URL. | `ext.py:63-71`, `auth.py:367-375` |
| **M** | **Generic password/token redaction truncates at the first embedded `&`/`;`/`,`.** The lazy `\S{n,m}?(?=\s|$|,|;|&)` stops inside the value; the suffix leaks. `password=sec&ret` → `[PASSWORD_REDACTED]&ret`. | `redaction_defaults.py:98-104` |
| **M** | **Recording files written with default umask (no 0o600).** Raw output (may contain un-redacted secrets) lands world-readable in a world-traversable dir (`/tmp/uterm-recordings`), bypassing the download-route authz. The project knows the pattern (`dev_idp.py` does `mkdir(0o700)+chmod(0o600)`). | `recording.py:137-138`, `session_logger.py:59-62` |
| **L** | `dev_token` JWT file written with default umask before `chmod 0o600` (TOCTOU world-readable admin token; loopback-only mitigates). | `dev_idp.py:111-120` |
| **L** | Detector annotations embed the matched (secret) text into recordings un-redacted (`log_event` does no redaction). | `runtime.py:276-286` _(uncertain — see limitations)_ |

### C. SSRF & egress control

| Sev | Finding | Location |
|---|---|---|
| **H** | **Governance/IDP/authz webhook URLs bypass the SSRF validator** that rigorously guards delivery webhooks. All four `GovernanceConfig` URLs + `AuthConfig.webhook_idp_url` are bare `str` with no validator; they receive the most sensitive data (keystrokes, auth headers) and could be pointed at `169.254.169.254`/internal hosts. | `config_schema.py:335-357`, `ext.py:74-76` |
| **H** | **Connector SSRF.** `POST /api/connect` forwards `host`/`port`/`url` verbatim to the ssh/telnet/ws connectors with no allow-list or private-IP block. Any operator-role principal pivots into the internal network / metadata service via the server's trust boundary. | `routes/tunnels.py:62-135` → `connectors/{ssh,telnet,websocket}.py` |
| **L** | **Delivery-webhook TOCTOU / DNS-rebinding residual.** `_delivery_url_allowed` resolves+validates, then `httpx.post` resolves *again* independently when it connects — a check-then-connect window the rebinding-defense comment claims to close but doesn't. (Independently confirmed by hand.) | `webhooks.py:292-339` |

> The three SSRF gaps share one root cause: **the SSRF validator is scoped to delivery webhooks only.**
> Factor it into a shared egress guard and apply it at config-load *and* connect-time to webhooks,
> governance URLs, connectors, and the MCP `url`.

### D. Concurrency & TOCTOU

| Sev | Finding | Location |
|---|---|---|
| **H** | **Approval resolve/reject TOCTOU → double command injection.** `approve_command`/`reject_command` check `PENDING`, then `await resolve_approval` (which injects the command at a yield point), then flip status. Two concurrent admin calls both pass the check; the held command is injected **twice**, or approved-and-rejected — defeating the human-in-the-loop gate. | `routes/approvals.py:54-81`, `core_impl.py:789-869` |
| **M** | **Resume-token single-use is not atomic.** `store.get(token)` then `store.revoke(token)` in separate transactions with an `await` yield between. Two connections presenting the same token both complete `get()` before either revokes → double role (up to admin) + hijack reclamation. | `resume.py:181-227`, `browser_handlers.py:396-406` |
| — | _(Independently noted)_ `try_acquire_rest` `await`s `worker_ws.send_text()` **while holding the hub's single global lock** (`lease.py:244`); a slow worker socket stalls lease ops for every session. Head-of-line blocking, not a correctness bug. | `lease.py:230-266` |

### E. Resource exhaustion / DoS / unbounded buffers

| Sev | Finding | Location |
|---|---|---|
| **H** | **Telnet IAC subnegotiation buffer unbounded (gateway).** `IAC SB` without a closing `IAC SE` makes `_sb_buf` grow by every inbound byte for the connection lifetime. The telnet gateway accepts **raw TCP with no pre-auth** → unauthenticated memory-amplification DoS. | `gateway/_iac_negotiate.py:219-231` |
| **M** | Same unbounded-`IAC SB` pattern in the **client** `TelnetTransport._rx_buf` (malicious upstream, reachable via `session_create` url/host). | `transports/telnet_transport.py:69,132-145` |
| **M** | **Paused-browser hold buffer unbounded** and *bypasses* the per-frame size check (append happens before the `max_input_chars` check). A held browser accumulates without limit; never reaped (the approval sweep never runs — see F). | `browser_handlers.py:225-235` |
| **M** | **Per-browser line buffer unbounded** under a configured policy gate — concatenates newline-free input with no cumulative cap; the gate is consulted only on the first chunk. | `store.py:84-92`, `browser_handlers.py:249` |
| **M** | **Hijack `expect_regex` is ReDoS-able.** Caller-supplied regex (200-char cap only) is `re.search`'d against the full screen in a poll loop, with none of the `_validate_pattern_safety`/length-truncation the SSE path uses. An authenticated hijacker pins a core / blocks the event loop. | `rest_helpers.py:40-68` |
| **M** | **Tunnel-worker WS has no message-size cap** (`receive_bytes()` with only a *minimum* check); oversized frames are JSON-parsed, broadcast, and appended to the event deque — 3× memory amplification. The regular worker route applies `max_ws_message_bytes`; this one doesn't. | `tunnel/fastapi_routes.py:104-135` |
| **M** | **No cap on hub worker/browser registrations.** WS routes `setdefault` on a caller-supplied `worker_id` with no per-principal quota and never consult `max_sessions`. One token holder opens thousands of unique-`worker_id` connections → OOM. | `bridge/hub/connection.py:136,259` |
| **M** | **Event ring stores full term deltas** (deque maxlen 2000); only per-frame (1 MiB) bound applies, so near-max frames can pin ~2 GiB per `worker_id`. | `router_impl.py:88-102`, `models.py:142` |
| **M** | **CF backpressure counter leaks permanently** on a failed browser send (`_queue_bytes += msg_len` then `await send_ws` raises → decrement skipped). Eventually `_queue_bytes > max_buffer_bytes` is always true → **all** frames silently dropped to every browser for that DO's lifetime. | `cloudflare/.../io.py:224-242` |
| **M** | **Control-plane tables grow forever** (soft-delete only; no `DELETE`/reaper/`VACUUM` anywhere). Every reconnect mints a permanent `cp_resume_tokens` row → slow disk-exhaustion DoS + retention/PII problem. | `sqlite/token_store.py:62-112` |
| **M** | **DeckMux `selection`/`pin` broadcast with no shape/size validation** (`setattr` of arbitrary JSON, re-shipped to every joiner). Mitigated to M by the 1 MiB WS cap + 10 fps control-bucket. | `deckmux/_service.py:201-221`, `_presence.py:72-82` |
| **L** | DeckMux `TransferManager` keystroke queue never cleared on disconnect (unbounded per-user leak). | `deckmux/_service.py:169-183` |

### F. Reliability & failure modes (fail-open / fail-closed)

| Sev | Finding | Location |
|---|---|---|
| **H** | **Approval expiry sweep is never scheduled in production** (`cleanup_expired` has only a test caller). Held commands never time out; the approval store + hold buffers leak. | `bridge/hub/approvals.py:69-92` |
| **M** | **Behavioral-audit & output-policy gates fail OPEN** on webhook error (`allow` / `return []`, explicitly commented "Default to allow on error"), unlike the policy/fanout gates which fail closed. A slow/unreachable webhook silently disables anomaly detection **and** redaction. (Output gate isn't production-wired today, which caps it at M.) | `ext.py:219-223,264-269` |
| **M** | **Webhook responses are never signature-verified inbound.** `verify_webhook_signature` (correct, timing-safe) is dead in production; IDP/authz providers trust the HTTP response's `subject_id/roles/scopes/allow` on TLS + a cleartext shared-secret header alone. | `webhook_signing.py:19-33`, `auth.py:385-395` |
| **M** | **CF JWKS fetch has no negative cache / stale-fallback.** On TTL expiry every request re-fetches; a transient IdP blip 401s all JWT traffic and stampedes the IdP (per-isolate cache, synchronized cliff). | `cloudflare/auth/jwt.py:70-104` |
| **L** | Worker reconnect loop never backs off / stops on persistent `1008` after accept → permanent low-frequency accept+close churn for a token-rotated fleet. | `worker_link.py:204-225` |
| **L** | Webhook delivery retries are un-jittered with a fresh `AsyncClient` per attempt → synchronized retry storm + per-attempt TLS handshake. | `webhooks.py:336-358` |
| **L** | EventBus drop-oldest queues silently shed audit/webhook events under load with no durable loss signal. | `event_bus.py:118-128` |

### G. Durability, audit & data integrity

| Sev | Finding | Location |
|---|---|---|
| **H** | **`/api/durability/capabilities` and the startup log overstate durability.** Both advertise `approvals` + `leases` as persisted in sqlite mode, but the factory only wires `ControlPlaneResumeStore` (resume tokens). Approvals use `InMemoryApprovalStore` unconditionally; leases live in in-memory `WorkerTermState`. Operators architecting failover on this advertised guarantee will silently lose in-flight approvals and all lease arbitration on restart. | `app/control_plane.py:54-71`, `factory_impl.py:152-157,635` |
| **M** | **Audit log has no ordering/sequence/tamper-resistance** and relies on non-monotonic `time.time()`. No hash-chain, no WORM sink; NTP step can reorder, gaps are undetectable. | `audit.py:40-55` |
| **M** | **Governance denials & IDP failures are not audited.** Policy `deny`, behavioral-kill, and webhook-IDP failures emit only `logger.warning` — exactly the events that signal an attack or a fail-open governance plane are absent from the structured audit trail. | `core_impl.py:816-824`, `router_impl.py:375-382`, `auth.py:396-405` |
| **L** | Recording `start_session` opens in append mode → reused-session recordings concatenate (replay integrity / stale less-redacted content survives the mtime sweep). | `recording.py:134-143` |

### H. Local privilege & platform tier (PAM / PTY / Cloudflare DO)

| Sev | Finding | Location |
|---|---|---|
| **H** | **PAM notify socket is unauthenticated & world-connectable** (no `chmod 0o600`, no `SO_PEERCRED` — contrast `CaptureSocket` which does chmod). Any local user forges login events that drive **root-side** session creation, fabricating audit records and spawning companion shells attributed to other users. | `pty/pam_listener.py:88` |
| **H** | **Root server binds a Unix socket at an attacker-controlled path** from the (unauthenticated) notify event — `ev.capture_socket` flows into `start_unix_server(path=...)` with only an "absolute + no null byte" check, no confinement to `cap_dir`. | `server/pam_integration.py:282`, `socket_utils.py:10` |
| **H** | **CF tunnel token revocation/rotation is not honored on a live DO.** Token hashes load once in `_ensure_meta` (gated by `_meta_loaded`); revoke/rotate writes new hashes to KV but the hot DO never re-reads them → a leaked share/control URL stays valid for the DO's lifetime (hours). | `cloudflare/.../session_runtime/auth.py:51-90`, `runtime.py:109-135` |
| **H** | **CF tunnel links silently break after DO hibernation** (same `_meta_loaded` root cause): `_restore_state` sets `_meta_loaded=True` from SQLite (which lacks the token hashes), suppressing the KV reload → all share/control URLs stop authenticating after the first eviction cycle. Self-inflicted availability outage. | `cloudflare/.../session_runtime/io.py:84-88` |
| **L** | Tunnel invite `issued_ip` is captured/stored but **never enforced** on redemption — `tunnel.ip_binding` gives a false sense of IP-pinning. | `server/tunnel_invites.py:44-58` |
| **L** | Token rotation/revocation does not force-close already-connected tunnel sockets (old session runs until peer disconnects). | `routes/tunnels.py:261-358` |
| **L** | _(Uncertain)_ PTY session with only a command (no username/run_as) execs as the server's euid (root) with no platform-layer authz — needs a human call on intended trust model. | `pty/connector.py:210` |

### I. Observability & operability

| Sev | Finding | Location |
|---|---|---|
| **M** | **Readiness probe always reports ready.** `/api/health` keys off `app.state.uterm_registry` (set *before* the lifespan runs migrations/tasks); `/healthz` is unconditionally 200. A half-initialized pod is added to the LB rotation; readiness can't drain it. | `routes/health.py:31-63`, `factory_impl.py:721,731` |
| **M** | **High-value load/attack signals are logged but never metricized** — rate-limit drops, REST 429s, webhook delivery failures + SSRF blocks, EventBus drops. No `rate_limit_*_total`/`webhook_*_total`/`events_dropped_total` exists, so on-call cannot alert on shedding/attack. | `websockets_impl.py:425-433`, `webhooks.py:342-359`, `event_bus.py:127` |
| **M** | **No startup TLS/scheme validation on any outbound URL** (governance/IDP/JWKS/recording/relay) — cleartext `http://` is silently accepted, so HMAC secrets / auth headers / the admin-minting JWKS traverse cleartext on an on-path network. | `config_schema.py:75-76,128,327-356` |
| **M** | **`security.mode="dev"` strips HSTS/CSP/X-Frame-Options with no non-loopback guard** (asymmetric with `auth.mode=dev_token`, which *does* refuse non-loopback). A config copied from dev silently disables clickjacking/HSTS/CSP on a routable server. | `security.py:31-34,47-49` |
| **M** | **CF worker bearer-token validation is presence-only** (no entropy/placeholder floor), while the FastAPI backend enforces a 32-char/known-placeholder floor — the *more exposed* backend is the *weaker* one. | `cloudflare/config.py:187-189` |
| **M** | Distributed trace context is never propagated outbound (no `traceparent` on webhooks/governance/IDP or across the WS bridge) → cross-service correlation breaks at every hop. | `webhooks.py:332-339` |
| **L** | Unauthenticated `/api/health` discloses version, control-plane backend, and live session count (pre-auth recon). | `routes/health.py:28-58` |
| **L** | REST hijack rate-limiting collapses to a single global bucket behind a reverse proxy (client-id indistinguishable) → one abuser denies everyone. | `routes/rest.py:112-121` |

### J. Supply chain & build

| Sev | Finding | Location |
|---|---|---|
| **H** | **The per-PR `pip-audit` gate audits zero packages and is always green.** `pip-audit --path .` filters by *install path* `.` (empty); the 168 installed packages live in `.venv`. Known fixable CVEs pass every PR. Fix: `uv run pip-audit` (active env) and assert a non-empty audited count. | `.github/workflows/ci.yml:73` |
| **M** | **`uv.lock` pinning is never enforced.** `Dockerfile.server` uses `uv pip install` (fresh resolution, ignores the lock); CI `uv sync` runs without `--frozen`; published packages ship only `>=` floors. The pinned graph is advisory. | `docker/Dockerfile.server:46,57`, `ci.yml` |
| **L** | orjson/ujson/stdlib JSON fallback in `control_channel.py` is **not byte-identical** (ujson escapes `/`, NaN/Inf vs null, exponent formatting), altering the frame header byte-length → cross-runtime frame drift that triggers the frontend raw-bytes fallback. Undeclared deps, tested only with fakes. | `control_channel.py:19-39` |
| L | `--changed-only` mutation gate returns 0 when no change touches the mutation roots; `container-scan` Trivy scans a non-reproducible (fresh-from-PyPI) image. | `run_mutation_gate.py:367`, `container-scan.yml` |

### K. Wire-protocol robustness (defensive depth)

| Sev | Finding | Location |
|---|---|---|
| **L** | Inbound frames are never validated through the canonical `AnyFrame` schema (it guards egress only); receive-path drift escapes the codegen check and `extra=forbid` is absent where untrusted input enters. | `websockets_impl.py` (receive path) |
| **L** | A single malformed worker frame field raises an uncaught `ValidationError` that tears down the **whole** worker session (entire-session blast radius from one bad frame). | `websockets_impl.py` (worker recv) |
| **L** | Worker status frame is re-broadcast verbatim with `extra=allow` → arbitrary unvalidated fields reach every browser (latent injection surface for future frontend code). | `bridge/frames.py:251-277` |
| **L** | Frontend `ProvideTerminal` writes the **raw, undecoded** WS payload (incl. control-frame bytes) to the terminal on any decode error — a worker/relay that crafts a guard-tripping frame pushes arbitrary bytes to the screen. | `frontend/terminal_impl.ts:305-314` |

---

## Fail-open vs fail-closed posture (per external dependency)

| Dependency | Behavior on failure | Correct? |
|---|---|---|
| JWT / JWKS (signature) | **Closed** (denies) | ✅ |
| Webhook IDP (`identity_provider=webhook`) | **Closed** by default (`webhook_idp_on_failure=deny`); `viewer` opt-in returns *anonymous* viewer, not the requester | ✅ (verify the `viewer` opt-in is intended) |
| Webhook **policy** / fanout gate | **Closed** (deny) | ✅ |
| Webhook **behavioral-audit** gate | **OPEN** (allow) | ❌ should be closed |
| Webhook **output-policy** (redaction) | **OPEN** (`return []` = no redaction) | ❌ should fall back to built-in rules |
| Approval webhook | Closed (buffered hold) | ✅ |
| SQLite control-plane (read) | Serializes; abandoned txn **deadlocks** the plane | ⚠ no timeout/watchdog |
| Upstream SSH/telnet | Connection error surfaces | ✅ |

The two ❌ rows are the priority: a *data-protection* control that fails open is the wrong default.

---

## What was checked and dismissed (evidence of rigor)

20 plausible findings were **refuted** by reading the code, including: oversized-frame decoder desync
(the decoder is reset on drop); tunnel-worker fail-open auth (the inline quirk is unreachable in the wired
path); intercept-`modify` request tampering (two separate WS connections, not one); CF JWKS `kty`/`alg`
confusion (WebCrypto `importKey` rejects non-RSA); PAM `LD_PRELOAD` env passthrough to root child (PAM only
runs when username+password supplied); `initgroups` silent-failure (it doesn't raise for unknown users);
recording secrets split across frame boundaries (snapshots pass whole screens); presence color CSS-injection
(values are coerced); identity-frame spoofing (the hub never calls the unsigned parser). This is why the
high floor is real — the codebase has already closed many of the obvious holes.

---

## Prioritized remediation roadmap

**P0 — close before any multi-tenant / internet-facing GA (small, surgical):**
1. **Fix the durability advertisement** — wire `SqliteApproval/LeaseStore`, or correct
   `durable_state`/the startup log to `('resume_tokens',)`. Add a test asserting every advertised store is
   actually instantiated. *(G-high)*
2. **Make the data-protection gates fail closed** — behavioral-audit → deny/hold; output-policy → built-in
   ruleset, not `[]`. Gate any fail-open behind an explicit flag. *(F-medium, but security-pivotal)*
3. **Unify SSRF/egress validation** — one guard applied at config-load + connect-time to governance/IDP
   URLs, connectors, and the MCP `url` arg. *(C-high ×3)*
4. **Redact `snapshot`/`analysis` frames** and mask no-echo keystrokes in recordings
   (`log_send_masked` when an echo-off prompt is active). *(B-high ×2)*
5. **Fix the `pip-audit` CI gate** (`uv run pip-audit`, assert non-empty count). *(J-high)*
6. **Authenticate the PAM notify socket** (`chmod 0o600` + `SO_PEERCRED`) and confine `capture_socket`
   to `cap_dir`. *(H-high ×2)*
7. **Atomic single-use** for approvals (compare-and-set under lock) and resume tokens
   (conditional `UPDATE ... WHERE revoked_at IS NULL`, require rowcount==1). *(D-high, D-medium)*

**P1 — enterprise hardening:**
- Per-send timeout on `broadcast()` + drop stalled viewers; move the worker `send_text` out of the global
  lock. *(E-high, lease HOL)*
- Cumulative caps on every accumulating buffer (hold/input line buffers, telnet IAC ×2, tunnel WS,
  event-ring per-worker byte budget); per-principal connection quota. *(E family)*
- CF: decouple token-hash loading from `_meta_loaded` (re-read on auth or short TTL). *(H-high ×2)*
- Manager: scoped tokens (worker-self-report vs operator); fail-closed embedder default. *(A-medium, A-low)*
- Audit completeness: emit `audit_event` for deny/behavioral-kill/governance-failure; add a monotonic
  sequence + optional hash-chain. *(G-medium ×2)*
- Add the missing metrics counters and a real readiness flag set at end-of-lifespan. *(I-medium ×2)*
- Startup `https://`-only validation for all outbound URLs; loopback guard on `security.mode=dev`; CF
  bearer-token entropy floor. *(I-medium ×3)*
- Control-plane reaper sweep + `VACUUM`; read path that doesn't take `BEGIN IMMEDIATE`; transaction
  watchdog. *(E-medium, F/SPOF)*

**P2 — architecture & scale:**
- Decide HA story: enforce single-active-instance (refuse to boot under k8s/ECS without `worker_id` sticky
  routing) **or** invest in shared control plane + cross-instance bus. Either way, stop letting a
  multi-replica deploy *silently* corrupt arbitration. *(K-architecture)*
- Converge the FastAPI/CF lease-auth implementations behind a shared conformance contract so security
  fixes land in both.
- Put `control/plane/sqlite/` on the mutation perimeter and give it a dedicated security pass (it's the
  only durable, security-critical backend and was outside the original scope).

---

## Coverage & limitations

- **Strong coverage:** auth, authz/lease/approval, hub concurrency, rate-limit/DoS, wire protocol,
  WS/HTTP routes, webhooks/audit/policy, tunnel/gateway/connectors, Cloudflare edge, PTY/PAM,
  recording/redaction, frontend, plus the supplementary manager/MCP/control-plane/DeckMux pass.
- **Thinner coverage (flagged for a follow-up human pass):** manager swarm-state JSON restore on startup
  (trust/permissions/schema-version of the rehydrated subprocess config); clock/time-source consistency
  (the codebase mixes `time.monotonic()` and `time.time()` for the same token lifetime); replay/JSONL
  deserialization bounds; multi-tenancy isolation guarantees end-to-end.
- **8 findings are "uncertain"** (code facts confirmed, exploitability needs a human judgment on intended
  trust model) — notably the root-euid PTY-with-only-a-command case and the control-channel `feed()`
  re-encode cost. These are listed in the raw output, not asserted as defects here.
- This review reads code; it did not execute exploits. P0 items should be reproduced before sign-off.
