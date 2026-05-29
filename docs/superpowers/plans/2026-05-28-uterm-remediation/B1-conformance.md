# Lane B1 — FastAPI ↔ Cloudflare Parity Conformance Suite (Wave B)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Read `00-ORCHESTRATION.md` "Global constraints" first.
>
> **DO NOT START until lanes A1 (Cloudflare) and A4 (Server) are merged and green.** This suite encodes the *corrected* behavior from those lanes; writing it earlier would pin the buggy behavior.

**Goal:** Build a backend-agnostic conformance suite that runs the SAME scenarios against both the FastAPI `TermHub` and the Cloudflare `SessionRuntime` Durable Object, so security/behavior parity is enforced going forward. This is the structural fix for the recurring "CF port silently diverged from server hardening" theme.

**Scope (exclusive write ownership):** new directory `tests/conformance/**` at repo root. Read-only against both packages. If a parity test reveals a NEW divergence bug, do NOT fix it here — file it as a cross-lane request to A1 or A4.

**Tech Stack:** pytest, parametrized fixtures, the existing CF DO test harness + FastAPI test client.

---

### Task 1: Backend abstraction fixture

**Files:**
- Create: `tests/conformance/__init__.py` (SPDX header)
- Create: `tests/conformance/conftest.py`
- Create: `tests/conformance/backends.py`

- [ ] **Step 1: Read** how each package is exercised in tests today: the FastAPI app factory + test client (server tests) and the CF DO/state harness (cloudflare tests). Identify the smallest common surface: auth-decision, lease acquire/heartbeat/release, event append/ordering.
- [ ] **Step 2:** Define a `ConformanceBackend` Protocol in `backends.py` with the common operations, e.g.:

```python
class ConformanceBackend(Protocol):
    async def decode_auth(self, headers: dict[str, str]) -> AuthOutcome: ...
    async def acquire_lease(self, worker_id: str, owner: str, ttl_s: float) -> bool: ...
    async def lease_active(self, worker_id: str) -> bool: ...
    async def append_event(self, worker_id: str, payload: dict) -> int: ...   # returns seq
    async def list_events(self, worker_id: str) -> list[dict]: ...
```
Implement `FastApiBackend` and `CloudflareBackend` adapters over the existing harnesses.

- [ ] **Step 3:** In `conftest.py`, expose a `backend` fixture parametrized over both: `@pytest.fixture(params=["fastapi", "cloudflare"])`.
- [ ] **Step 4: Commit** — `test(conformance): add dual-backend conformance fixture`

---

### Task 2: Auth parity tests

**Files:** Create `tests/conformance/test_auth_parity.py`.

Encodes the corrected A1/A4 behavior:
- [ ] **Test:** neither backend grants any role for `dev`/`none` mode (A1 removed it; the server never had it). Both must reject/deny.
- [ ] **Test:** both backends reject a config mixing `HS*` with an asymmetric key (the ALG guard) — shared spec from orchestration.
- [ ] **Test:** an unsigned/expired/bad-`aud` token is denied by both.
- [ ] **Test:** a valid token yields the same role mapping on both.
- [ ] **Step: Run** `uv run pytest tests/conformance/test_auth_parity.py -q` — all params green.
- [ ] **Commit** — `test(conformance): assert auth parity across backends`

---

### Task 3: Lease state-machine parity tests

**Files:** Create `tests/conformance/test_lease_parity.py`.

- [ ] **Test:** acquire → `lease_active` true on both.
- [ ] **Test:** after TTL elapses (advance wall clock), `lease_active` false on both — **this is the CB-CF-lease regression guard**: simulate a restart/rehydrate between acquire and check and assert the lease expiry is judged by wall-clock, identically on both backends.
- [ ] **Test:** concurrent acquire yields a single winner on both.
- [ ] **Test:** release frees the worker on both.
- [ ] **Run** + **Commit** — `test(conformance): assert hijack-lease parity across backends`

---

### Task 4: Event ordering / sequence parity

**Files:** Create `tests/conformance/test_events_parity.py`.

- [ ] **Test:** appended events get monotonically increasing `seq` on both; `list_events` returns the same order.
- [ ] **Test:** the same oversized-frame / cap behavior on both (each enforces its `max_*` limits consistently).
- [ ] **Run** + **Commit** — `test(conformance): assert event ordering parity across backends`

---

### Task 5: Wire into CI

**Files:** This touches CI config owned by **A6** — file a cross-lane request rather than editing `.github/` here, OR (if A6 has already merged) coordinate with the orchestrator to add `tests/conformance` to the test matrix and to `testpaths`/`run_all_tests.py`.

- [ ] **Step 1:** Ensure `tests/conformance` is collected by `scripts/run_all_tests.py` (request A6/orchestrator to add it).
- [ ] **Step 2:** Confirm the suite runs in CI on both backends.

---

### Done criteria (Lane B1)
- [ ] `uv run pytest tests/conformance/ -q` green on BOTH params
- [ ] `uv run ruff check --fix && uv run ruff format && uv run mypy tests/conformance/` (if mypy covers tests here)
- [ ] Any divergence discovered is filed as a cross-lane request, not silently fixed here.
- [ ] Commits, one logical unit each.

### Cross-lane requests
- **A6 / orchestrator:** register `tests/conformance` in `scripts/run_all_tests.py` and the CI matrix.
- **A1 / A4:** any new divergence the suite surfaces.
