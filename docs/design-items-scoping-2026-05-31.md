# Design-items scoping (2026-05-31)

This is a synthesis task — the scoping JSON is already provided. I'll write the markdown directly.

## 1f/1d — IDP Webhook Contract Hardening

**Problem.** `WebhookIdentityProvider.resolve_principal` trusts the IDP's JSON response with no signature check (1f), so anyone who can answer at the URL (DNS/BGP hijack, mis-issued TLS cert, compromised sidecar, SSRF redirect, look-alike URL) returns `{"subject_id":"attacker","roles":["admin"]}` and is authenticated as admin; and it forwards the full header + cookie map (1d), shipping the bearer token, all session cookies, CSRF tokens and `X-Api-Key` to a third party on every request.

**Today.** `packages/provide-uterm-server/src/provide/uterm/server/auth.py:385-448` — builds `headers=dict(connection.headers)` / `cookies=dict(connection.cookies)` (full maps, `:393-394`); signs the *outbound* body only (`:402-407`); POSTs and does `resp.json()` with **no** response-signature check (`:418-420`); builds `Principal` straight from `data` (`:425-431`). A timing-safe `verify_webhook_signature` already exists at `webhook_signing.py:23` but is unused inbound. Config in `config_schema.py` AuthConfig (`:40-103`) has no verify flag and no header-forward field; provider wired at `app/factory_impl.py:467-473`. Tests `tests/server/test_webhook_idp.py` (11 tests) assume an unsigned response and full header forward.

**Options.**

| Option | What it does | Effort | Risk | Recommended? |
|---|---|---|---|---|
| A. Implicit verify + allow-list | Verify response only when a secret is set; forward a curated header/cookie allow-list | M | Verification is silent/optional — same fail-open foot-gun class | No |
| B. Explicit `require_signed` flag + allow-list | `webhook_idp_require_signed_response=True` default; config validator fails startup if no secret; curated allow-list + optional override list | M | One extra knob; fails closed at startup (intended) | **Yes** |
| C. Dedicated credential, no raw headers | Send only extracted bearer + named cookie; mandatory verify | M | Hard-codes *where* the credential lives; breaks non-bearer IDPs | No (defer) |

**Recommendation.** Option B — default-secure, mis-config fails at startup not silently, reuses `verify_webhook_signature`, routes invalid signatures through the existing except→audit→deny/viewer path.

**Decision needed from you.** **(1f) Require signed IDP responses by default with a startup-failing validator (B), or only verify when a secret happens to be set (A)? (1d) Forward a curated credential allow-list with an override (A/B), or go further and send only an extracted bearer + named cookie, dropping all raw headers (C, more breaking to the IDP contract)?**

## 5a — Audit-Log Tamper-Resistance

**Problem.** `audit_event()` emits records with a non-monotonic `time.time()` stamp, no sequence number, no hash-chain and no append-only sink — so an NTP step reorders events, dropped records are undetectable, and anyone with write access to the log sink can delete or rewrite their `auth.success`/`session.create` entries undetectably. Prevents post-compromise log tampering.

**Today.** `packages/provide-uterm-server/src/provide/uterm/server/audit.py:40-55` — thin sync wrapper logging via `_audit_log = get_logger("provide.uterm.audit")` (`:14`) with `ts=time.time()`, no seq/chain. 21 call sites across 5 files; `auth.success`/`auth.failure` are on the per-request hot path (`auth.py:190,193,300,334`). Threading.Lock precedent at `auth.py:68`. No audit config section; control plane is single-active. `audit.py` not yet on the mutmut perimeter; `test_audit.py` asserts exact extra-dict keys (additions safe, renames break).

**Options.**

| Option | What it does | Effort | Risk | Recommended? |
|---|---|---|---|---|
| A. Seq + monotonic clock | Process-global `seq` + `mono_ns` + `pid_boot`; detects reorder/gaps | S | No content-tamper proof — edits to a record (incl. its seq) stay invisible | Phase 1 |
| B. Seq + sha256 prev-hash chain | A + per-event `hash = H(prev‖canonical(fields))` under the lock; any edit/insert/delete/reorder of a retained stream is provable + pinpointable | M | µs sha256, no hot-path I/O; per-process chain resets on restart (tagged, single-active OK); no whole-sink-deletion defense | **Yes** |
| C. B + append-only WORM file | B + O_APPEND 0600 file, background writer, periodic anchors vs tail-truncation | L | Config + thread lifecycle + ops (no copytruncate); app file still root-deletable; real immutability is infra | No (unless compliance) |

**Recommendation.** Option B — smallest change that actually closes "a tampered log is indistinguishable"; zero call-site/config changes, reuses the `auth.py:68` lock pattern. Land A as commit 1, the chain as commit 2.

**Decision needed from you.** **Which tamper-evidence level: (A) seq + monotonic only, (B) seq + sha256 prev-hash chain (recommended), or (C) B + an app-managed append-only WORM file? And is per-process chain state (resets on restart/fork, justified by single-active control plane) sufficient, or do you require cross-restart / cross-process continuity — which pushes toward C or an external durable store?**

## 5b — Manager Scoped Tokens

**Problem.** `uterm-manager`'s `TokenAuthMiddleware` gates every route with one `hmac.compare_digest` against a single shared bearer token — so the high-frequency worker self-report path (`POST /agent/{id}/status`) needs the same omnipotent token that authorizes spawn, kill-all, prune, DELETE agent, restart. One leaked worker token = fleet-wide command-and-control. Workers are the most numerous, exposed, least-trusted holders.

**Today.** `packages/provide-uterm-platform/src/provide/uterm/manager/auth.py:20-96` — single `self._token`, one compare at `:86`; already has `scope['path']`/`scope['method']` in hand (`:76,:61`) but uses them only for public-path bypass (`:48-50`). `setup_auth()` (`:114-170`) reads one env var (`UTERM_MANAGER_API_TOKEN`, configurable via `config.py:40`). All routes on one shared APIRouter (`routes/models.py:17`); no route declares privilege. `process_impl.py:235` forwards `UTERM_`-prefixed env to spawned workers. `auth.py` not on the mutmut perimeter but `test_auth.py`/`test_auth_mutant_kills.py` exercise it at mutation grade.

**Options.**

| Option | What it does | Effort | Risk | Recommended? |
|---|---|---|---|---|
| A. Two static tokens | Low-priv `worker_token` for self-report routes (`POST /agent/{id}/status` + `/register`) + existing operator token for all else; middleware classifies by path | S | Mis-classify a route; leaked worker token can still self-report as *any* agent_id | **Yes** |
| B. Scope-tagged token map | token→scopes map; each route declares a required scope; default-deny | M | New config shape + route-scope source of truth; still shared secrets | If >2 tiers expected |
| C. Per-worker capability tokens | `HMAC(secret, agent_id)` minted in `_build_worker_env`; self-report route verifies token binds to the path's agent_id | L | Path-template parsing + middleware/dependency split + spawn-path coupling (process_impl is on mutmut perimeter) | Follow-up to A |

**Recommendation.** Option A now — closes the fleet-takeover threat with the smallest, backward-compatible change (operator token unchanged, worker token optional); layer C later if cross-agent self-report forgery is in scope.

**Decision needed from you.** **(A) two static tokens, (B) scope-tagged token map, or (C) per-worker HMAC capability tokens? And: should the worker-scope allowlist be just `POST /agent/{id}/status` (+`/register`), or also the GET read paths (`/agent/{id}/status|details|events`, `/swarm/status`)?**

## 5d — Inbound Worker-Frame Validation

**Problem.** The worker WS receive handler decodes frames with JSON-only guards and reads fields via `dict.get()` + `cast()` (a runtime no-op), so types are never checked at the trust boundary; one malformed field (e.g. snapshot `cursor.x = "abc"`) raises `ValidationError` in the frame builder, hits only the outer `except`, and tears down the worker session **and every browser viewing it** — DoS from one bad frame.

**Today.** `packages/provide-uterm-server/src/provide/uterm/server/bridge/routes/websockets_impl.py:87-318` — `decoder.feed()` does JSON parse + depth/size only (`control_channel.py`, no schema check); for-event loop `:150-270` has **no** per-frame try/except, only the outer `except Exception` at `:273` that breaks the loop and deregisters the worker. Snapshot path `:231-254` is cast-only and `SnapshotFrame(extra=forbid)` raises on bad type. Browser handler (`:319-590`, except at `:507`) has the identical shape. `AnyFrame` (`schemas.py:337`) validates nothing at runtime. **Drift:** `WorkerHelloFrame` (`schemas.py:151`) declares only `mode`/`ts` but the real wire carries `input_mode`/`protocol`/`protocol_version`, so naive full-AnyFrame validation would reject every real hello. Metric hook: `hub.metric()` (`hub/store.py:135`).

**Options.**

| Option | What it does | Effort | Risk | Recommended? |
|---|---|---|---|---|
| A. Drop-bad-frame (lenient / extra=ignore) | Per-frame try/except: drop the bad frame, emit `ws_worker_frame_invalid_total` + rate-limited warn, keep session alive; hot-path DataChunk stays outside the wrapper | S | New drop-branch coverage/mutation tests; over-broad except masks bugs (catch specific types) | **Yes** |
| B. Reject-session (full AnyFrame, extra=forbid) | Validate via `AnyFrame`; on invalid send error frame + close 1003 | M | Currently broken (WorkerHelloFrame drift → disconnects all real workers); hostile to forward-compat; punishes viewers for one worker's bug | No |
| C. Lightweight shape-guard | Hand-written per-type check of load-bearing fields only; pair with A's try/except | S | Second source of truth vs schemas.py → drift; only guards remembered fields | Viable alt for the validation layer |

**Recommendation.** Option A (drop-bad-frame, lenient/`extra=ignore`) — the per-frame try/except removes the whole-session DoS at near-zero risk and should ship regardless; defer full-AnyFrame-strict until WorkerHelloFrame/SnapshotFrame drift is corrected.

**Decision needed from you.** **(1) On a malformed worker frame: DROP it and keep the session alive (A), or REJECT/close with a protocol-error code (B)? (2) For validation: full AnyFrame Pydantic vs a hand-written shape-guard (C), and `extra=forbid` (strict, rejects newer worker fields) vs `extra=ignore` (forward-compatible)?**

---

**Suggested order (value÷effort):** 5d-A (S, kills a live DoS) → 5b-A (S, fleet-takeover) → 5a-A then B (S→M, audit integrity) → 1f/1d-B (M, IDP contract).

---

## DECISIONS (2026-05-31)

- **5a — audit:** FULL WORM/compliance build. Seq + monotonic clock + sha256 prev-hash chain + chain head persisted to the control-plane store (restart/HA-ready) + a `verify-audit-log` verifier + periodic anchor records + an app-managed append-only (O_APPEND, 0600) audit file with anchoring-vs-truncation detection. Document "route the audit file/sink to retention-locked/WORM storage" as the ops requirement (real immutability is infra). Cross-instance anchoring deferred to the P2 HA decision. (L effort — own cluster.)
- **5d — inbound frames:** runtime config flag `worker_frame_on_invalid: "drop" | "reject"` (default `drop`). Per-frame try/except; the except drops+metrics (`ws_worker_frame_invalid_total`) or sends an error frame + closes 1003 per the flag. Validate via `AnyFrame` with `extra=ignore` (forward-compatible). Hot-path DataChunk stays outside the wrapper.
- **5b — manager tokens:** two static tokens. Low-priv `worker_token` authorizes ONLY `POST /agent/{id}/status` + `/register`; the existing operator token authorizes everything else (spawn/kill/delete/restart + GET reads). Middleware classifies by path.
- **1f/1d — IDP contract:** require a signed IDP response by DEFAULT (`webhook_idp_require_signed_response=True`); a startup validator fails boot if required-but-no-secret; verify the response with the existing timing-safe `verify_webhook_signature`; forward only a curated header/cookie allow-list (bearer + configured auth cookie) with an optional override list. PLUS a **dev-mode opt-out** (loopback-gated or an explicit acknowledged flag, mirroring `dev_token`/`security.mode=dev`) that lets local dev run unsigned.

**Implementation order:** finish remediation R1–R5 first, then design items in value÷effort order: 5d → 5b → 1f/1d → 5a.
