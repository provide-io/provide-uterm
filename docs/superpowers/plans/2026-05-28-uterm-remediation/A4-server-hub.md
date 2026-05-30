# Lane A4 — Server / Hub Correctness & Hardening Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Read `00-ORCHESTRATION.md` "Global constraints" first. Hub services are on the **mutation perimeter** — run the mutation gate before done.

**Goal:** Fix the dead-socket leak, the rate-limiter eviction defect, a broadcast hot-path scaling issue, and two auth-surface hardening items in the FastAPI server.

**Scope (exclusive write ownership):** `packages/provide-uterm-server/**` only.

**Tech Stack:** Python, FastAPI, asyncio WebSockets, pytest.

**Order:** CB-2 → SRV-rl → ALG → SRV-share → SRV-cookie → SRV-bcast.

---

## Tasks

### Task 1 (CB-2 🟠): Discard `_paused_browsers` on disconnect

**Files:**
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/core_impl.py:505-516` (`cleanup_browser_disconnect`)
- Test: `packages/provide-uterm-server/tests/bridge/` hub test module

**Problem:** `cleanup_browser_disconnect` pops `_input_buffers` and `_hold_buffers` but NOT `_paused_browsers` (only `resolve_approval` at `:825-826` discards it). A browser disconnecting while an approval is pending leaks a dead `WebSocket` into the set forever.

- [ ] **Step 1: Read** `cleanup_browser_disconnect` (505-516), `_paused_browsers` init (`:634`), and the add sites (`browser_handlers.py:263,313`).
- [ ] **Step 2: Write failing test:**

```python
async def test_disconnect_clears_paused_browser(hub, fake_ws):
    hub._paused_browsers.add(fake_ws)
    hub._hold_buffers[fake_ws] = "queued"
    hub.cleanup_browser_disconnect(fake_ws)
    assert fake_ws not in hub._paused_browsers
    assert fake_ws not in hub._hold_buffers
```

- [ ] **Step 3: Run, expect FAIL.** `uv run pytest packages/provide-uterm-server/tests/bridge/ -k paused -v`
- [ ] **Step 4: Implement.** Add alongside the existing pops (both branches, ~509 and ~516):

```python
self._paused_browsers.discard(ws)
```
- [ ] **Step 5: Run, expect PASS** + `uv run pytest packages/provide-uterm-server/tests/ -q`.
- [ ] **Step 6: Commit** — `fix(server): discard paused browser on disconnect to prevent dead-socket leak`

---

### Task 2 (SRV-rl 🟡): Fix rate-limiter eviction (evict-after-insert; don't drop the inserting key)

**Files:** Modify `packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/limiter.py:140-169` (`allow_rest_acquire`, `allow_rest_send`, `_evict_if_full`). Test: limiter test module.

**Problem:** Eviction runs *before* `setdefault`, evicting the first half by insertion order. When the cache is full, the current client's bucket can be in the evicted half and then immediately recreated with a full token allotment — letting a client that churns the cache (≥`REST_CLIENT_CACHE_MAX` distinct IPs) reset its own limit. It is also FIFO, not LRU, despite the docstring.

- [ ] **Step 1: Read** the two `allow_*` methods and `_evict_if_full`.
- [ ] **Step 2: Write failing test:**

```python
def test_eviction_never_drops_the_inserting_client():
    lim = RateLimiter(...)
    # Fill to cap with distinct ids.
    for i in range(REST_CLIENT_CACHE_MAX):
        lim.allow_rest_acquire(f"c{i}")
    # Drain victim's bucket to 0, then force an overflow insert by the SAME victim.
    victim = "c0"
    while lim.allow_rest_acquire(victim):
        pass
    # Trigger overflow with a new id, then victim again — victim must NOT come back refilled.
    lim.allow_rest_acquire("new-id-forcing-evict")
    assert lim.allow_rest_acquire(victim) is False  # bucket state survived; not reset
```

- [ ] **Step 3: Run, expect FAIL.**
- [ ] **Step 4: Implement.** `setdefault` first (so the active key exists and moves to the end), then evict, and never evict the key just inserted:

```python
def allow_rest_acquire(self, client_id: str) -> bool:
    bucket = self._rest_acquire_per_client.setdefault(client_id, TokenBucket(self._rest_acquire_rate))
    self._rest_acquire_per_client.move_to_end(client_id)  # requires OrderedDict
    self._evict_if_full(self._rest_acquire_per_client, keep=client_id)
    return bucket.allow() and self._rest_acquire_bucket.allow()
```
Change the two per-client dicts to `OrderedDict` (init site), and update `_evict_if_full` to skip `keep` and trim from the front (true LRU now that `move_to_end` is called on access):

```python
@staticmethod
def _evict_if_full(per_client: "OrderedDict[str, TokenBucket]", *, keep: str) -> None:
    if len(per_client) > REST_CLIENT_CACHE_MAX:
        for k in list(per_client)[:REST_CLIENT_EVICT_COUNT]:
            if k != keep:
                del per_client[k]
```
Apply the same to `allow_rest_send`. Update the docstring (it is now real LRU).

- [ ] **Step 5: Run, expect PASS** + suite green.
- [ ] **Step 6: Commit** — `fix(server): make REST rate-limiter eviction true LRU and never reset the active client`

---

### Task 3 (ALG 🟡): JWT algorithm-confusion startup guard (server half)

**Files:** Modify the server JWT config validator (find via `git grep -n "jwt_algorithms\|jwt_public_key_pem" packages/provide-uterm-server/src` — likely `server/app/auth.py` or a config module). Test: server auth test module.

**Problem & rule:** See `00-ORCHESTRATION.md` "Shared spec". Reject configs mixing `HS*` with asymmetric algs or with a PEM/JWKS key. Implement identically to A1's CF copy.

- [ ] **Step 1: Read** the server's JWT config loading/validation.
- [ ] **Step 2: Write failing test:**

```python
import pytest
def test_jwt_config_rejects_hmac_with_public_key():
    with pytest.raises(ValueError, match="algorithm"):
        build_jwt_settings(algorithms=["RS256", "HS256"], public_key_pem="-----BEGIN PUBLIC KEY-----\n...")
```

- [ ] **Step 3: Run, expect FAIL.**
- [ ] **Step 4: Implement** the guard (same logic as A1 Task 3) at config-validation time:

```python
HMAC_ALGS = {"HS256", "HS384", "HS512"}
if any(a in HMAC_ALGS for a in algorithms) and (
    any(a not in HMAC_ALGS for a in algorithms) or public_key_pem or jwks_url
):
    raise ValueError("jwt_algorithms must not combine HMAC (HS*) with asymmetric algorithms or a public key")
```
- [ ] **Step 5: Run, expect PASS** + suite green.
- [ ] **Step 6: Commit** — `fix(server): reject HMAC+asymmetric JWT config (algorithm confusion)`

---

### Task 4 (SRV-share 🟢): Scope tunnel share-operator principal to its session

**Files:** Modify `packages/provide-uterm-server/src/provide/uterm/server/.../authorization.py:114-115` and `factory_impl.py:255-259`. Test: authorization test module.

**Problem:** Share-operator principals carry global `roles=admin` and `is_admin()` is role-global. Cross-session escalation is currently prevented only because the principal is minted per-request from the path's `session_id`; any future change that resolves it independently of the path becomes a cross-session admin escalation. Remove the footgun by scoping the grant to the session.

- [ ] **Step 1: Read** how the share principal is minted and how `is_admin()`/authz consume roles.
- [ ] **Step 2: Write failing test:** a share-operator principal minted for session A must NOT pass an admin/authz check for session B.

```python
def test_share_operator_admin_is_session_scoped():
    p = mint_share_operator_principal(session_id="A")
    assert authz_allows(p, action="admin_op", session_id="A") is True
    assert authz_allows(p, action="admin_op", session_id="B") is False
```

- [ ] **Step 3: Run, expect FAIL.**
- [ ] **Step 4: Implement.** Replace the global `admin` role with a session-scoped capability (e.g. `roles=("operator",)` plus a `session_scope="A"` field, or a `ScopedAdmin(session_id="A")` grant). Update the authz check to require the action's `session_id` to match the principal's scope. Keep existing behavior for session A.
- [ ] **Step 5: Run, expect PASS** + suite green.
- [ ] **Step 6: Commit** — `fix(server): scope tunnel share-operator admin grant to its session`

---

### Task 5 (SRV-cookie 🟢): Stop deriving cookie `secure` from `x-forwarded-proto`

**Files:** Modify `packages/provide-uterm-server/src/provide/uterm/server/app/routes_wiring.py:92-98`. Test: routes/cookie test module.

**Problem:** The share cookie's `secure` flag is derived from the spoofable `x-forwarded-proto` header.

- [ ] **Step 1: Read** how `secure` is currently computed (~92-98) and whether a trusted-proxy posture or static TLS config flag exists.
- [ ] **Step 2: Write failing test:** with `x-forwarded-proto: https` from an untrusted peer (no trusted-proxy config), the cookie must NOT be marked `secure` based on the header alone; with a static `cookie_secure=true` config it is.
- [ ] **Step 3: Run, expect FAIL.**
- [ ] **Step 4: Implement.** Derive `secure` from a static config flag (default per deployment) or a trusted-proxy-validated source — not the raw header. Add the flag to the server config module (no hardcoded value inline).
- [ ] **Step 5: Run, expect PASS** + suite green.
- [ ] **Step 6: Commit** — `fix(server): derive cookie secure flag from config, not x-forwarded-proto`

---

### Task 6 (SRV-bcast 🟢): Build policy context once per frame in `broadcast`

**Files:** Modify `packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/router_impl.py:106-143` (`broadcast`). Test: router test module + a micro-benchmark assertion.

**Problem:** When an `_output_policy_gate` is configured, `broadcast` calls `prepare_policy_context` (which re-acquires `hub._lock`, `store.py:223`) and `get_redaction_rules` **per browser per frame**. N viewers ⇒ N policy builds + N lock acquisitions per terminal frame → throughput collapse.

- [ ] **Step 1: Read** `broadcast` (106-143) and `prepare_policy_context`/`get_redaction_rules`.
- [ ] **Step 2: Write failing test:** spy on `prepare_policy_context`; broadcasting one frame to 5 viewers with a gate active must call it **once**, not 5×.

```python
async def test_broadcast_builds_policy_context_once_per_frame(hub, monkeypatch):
    calls = 0
    orig = hub.prepare_policy_context
    def spy(*a, **k):
        nonlocal calls; calls += 1; return orig(*a, **k)
    monkeypatch.setattr(hub, "prepare_policy_context", spy)
    # register 5 viewers + an output gate, then broadcast one frame
    await hub.broadcast(worker_id="w", data=b"x")
    assert calls == 1
```

- [ ] **Step 3: Run, expect FAIL.**
- [ ] **Step 4: Implement.** Hoist `prepare_policy_context` + `get_redaction_rules` out of the per-browser loop: compute once per (worker, frame) before the loop and reuse for all viewers. If redaction differs per viewer-role, build a small per-role cache for that frame rather than per-browser. Preserve correctness of who-sees-redacted.
- [ ] **Step 5: Run, expect PASS** + suite green.
- [ ] **Step 6: Commit** — `perf(server): build broadcast policy context once per frame`

---

### Done criteria (Lane A4)
- [ ] `uv run pytest packages/provide-uterm-server/tests/ -q` green
- [ ] `uv run ruff check --fix && uv run ruff format && uv run mypy packages/provide-uterm-server/src/`
- [ ] `uv run python scripts/run_mutation_gate.py --changed-only --min-mutation-score 100` → 0 survivors on touched hub/auth files
- [ ] 6 commits, one per task. Signal A4 complete so B1 can start.
- [ ] If A2 filed a cross-lane request for server-side regex matching bounds, address it (add a length cap where the server compiles `expect_regex`/`pattern`) as an extra task + commit.

### Cross-lane requests
_(record any out-of-scope change needed here)_
