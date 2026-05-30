# Lane A1 — Cloudflare Worker Security Remediation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Read `00-ORCHESTRATION.md` "Global constraints" first. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close the Cloudflare worker's auth bypass and lease-clock bugs so the CF backend matches the FastAPI server's hardened auth model.

**Scope (exclusive write ownership):** `packages/provide-uterm-cloudflare/**` only. Do NOT touch root files or other packages. CF auth files are on the **mutation perimeter** — run the mutation gate before done.

**Tech Stack:** Python (Pyodide-targeted), pytest, PyJWT (test path), Web Crypto (runtime path).

**Order:** CB-3 → CF-svc → ALG → CF-lease (security first, clock bug last).

---

## Tasks

### Task 1 (CB-3 🔴 Critical): Remove `dev`/`none` admin bypass from the worker

**Files:**
- Modify: `packages/provide-uterm-cloudflare/src/provide/uterm/cloudflare/auth/jwt.py:269-271`
- Modify: `packages/provide-uterm-cloudflare/src/provide/uterm/cloudflare/config.py:99-104`
- Test: `packages/provide-uterm-cloudflare/tests/` (locate the JWT/auth test module, e.g. `test_jwt*.py`)

**Problem:** `decode_jwt` returns `Principal(subject_id="dev", roles=("admin",))` whenever `config.mode in {"none","dev"}`, gated only by a production-environment check in `config.py`. The default `ENVIRONMENT=development` leaves an internet-facing worker open as admin. `CLAUDE.md` states these modes were removed server-side; CF must match.

- [ ] **Step 1: Read** `auth/jwt.py` (the `decode_jwt` function ~line 269) and `config.py` `from_env` (~95-110) to see how `mode` is parsed and validated.

- [ ] **Step 2: Write failing tests.** Add to the CF auth test module:

```python
def test_jwt_config_rejects_dev_and_none_modes():
    import pytest
    from provide.uterm.cloudflare.config import _build_jwt_config_or_raise  # adjust to real factory
    for mode in ("dev", "none"):
        with pytest.raises(ValueError, match="AUTH_MODE"):
            _build_jwt_config_or_raise({"AUTH_MODE": mode})

async def test_decode_jwt_has_no_dev_bypass(monkeypatch):
    # decode_jwt must never mint an admin principal without a verified token.
    from provide.uterm.cloudflare.auth import jwt as jwtmod
    cfg = jwtmod.JwtConfig(mode="none")  # if JwtConfig can still be constructed directly
    with pytest.raises(jwtmod.JwtValidationError):
        await jwtmod.decode_jwt("", cfg)
```
(Adjust import paths/constructors to the real API after Step 1.)

- [ ] **Step 3: Run, expect FAIL.** `uv run pytest packages/provide-uterm-cloudflare/tests/ -k "dev or none or bypass" -v`

- [ ] **Step 4: Implement.** In `config.py`, reject `dev`/`none` for ALL environments (not just production):

```python
mode = _get("AUTH_MODE", "jwt").strip().lower() or "jwt"
if mode not in {"jwt"}:
    raise ValueError("AUTH_MODE must be 'jwt' (dev/none modes are removed; the worker is always internet-facing)")
```
In `auth/jwt.py`, delete the bypass branch entirely:
```python
async def decode_jwt(token: str, config: JwtConfig) -> Principal:
    if not config.public_key_pem and not config.jwks_url:
        raise JwtValidationError("missing jwt public key")
    ...
```
Remove the now-dead `is_production` branching in `config.py` if it only existed for dev/none. Grep the package for any test fixture that constructs `mode="dev"/"none"` and update it to a real signed-token fixture.

- [ ] **Step 5: Run, expect PASS** + the existing CF suite stays green: `uv run pytest packages/provide-uterm-cloudflare/tests/ -q`

- [ ] **Step 6: Commit** — `fix(cloudflare): remove dev/none auth bypass from worker`

---

### Task 2 (CF-svc 🟡): Tighten CF Access service-token auto-admin

**Files:** Modify `packages/provide-uterm-cloudflare/src/provide/uterm/cloudflare/auth/jwt.py:286-296`; same test module.

**Problem:** Any validly-signed token with empty `sub` + non-empty `common_name` is auto-granted `admin` as a "service token". Empty `sub` alone is too weak a signal.

- [ ] **Step 1: Read** the `is_service_token` block (~286-296).
- [ ] **Step 2: Write failing test:** a signed token with empty `sub`, a `common_name`, but NO service-token marker claim must be rejected (or downgraded to no roles), not granted admin.

```python
async def test_empty_sub_without_service_marker_is_not_admin(signed_token_factory):
    cfg = ...  # JWKS/PEM-backed config
    token = signed_token_factory(sub="", common_name="acme", typ="user")  # no svc marker
    with pytest.raises(JwtValidationError):
        await decode_jwt(token, cfg)
```

- [ ] **Step 3: Run, expect FAIL.**
- [ ] **Step 4: Implement.** Require an explicit CF Access service-token signal (the standard CF Access service-token JWTs carry a distinguishing claim — verify against CF docs; commonly the issuer path `/cdn-cgi/access` audience or a `"type":"app"` / absence of `email` with presence of a service-token-specific claim). Gate `is_service_token` on that explicit claim AND non-empty `common_name`, not on `sub == ""` alone. If the signal is absent and `sub` is empty, raise `JwtValidationError("missing sub")`.
- [ ] **Step 5: Run, expect PASS** + full CF suite green.
- [ ] **Step 6: Commit** — `fix(cloudflare): require explicit service-token signal for admin grant`

---

### Task 3 (ALG 🟡): JWT algorithm-confusion startup guard (CF half)

**Files:** Modify `packages/provide-uterm-cloudflare/src/provide/uterm/cloudflare/config.py` (the JWT config builder); same test module.

**Problem & rule:** See `00-ORCHESTRATION.md` "Shared spec — JWT algorithm-confusion guard". Reject configs mixing `HS*` with asymmetric algs or with a PEM/JWKS key.

- [ ] **Step 1: Read** how `algorithms`, `public_key_pem`, `jwks_url` are parsed in `config.py`.
- [ ] **Step 2: Write failing test:**

```python
def test_jwt_config_rejects_hs_mixed_with_asymmetric_key():
    import pytest
    for algs in ("RS256,HS256", "HS256"):
        with pytest.raises(ValueError, match="algorithm"):
            _build_jwt_config_or_raise({
                "AUTH_MODE": "jwt", "JWT_ALGORITHMS": algs,
                "JWT_PUBLIC_KEY_PEM": "-----BEGIN PUBLIC KEY-----\n...",
            })
```

- [ ] **Step 3: Run, expect FAIL.**
- [ ] **Step 4: Implement** the guard after parsing algorithms + key material:

```python
HMAC_ALGS = {"HS256", "HS384", "HS512"}
has_hmac = any(a in HMAC_ALGS for a in algorithms)
has_asym = any(a not in HMAC_ALGS for a in algorithms)
has_pub_key = bool(public_key_pem or jwks_url)
if has_hmac and (has_asym or has_pub_key):
    raise ValueError("JWT_ALGORITHMS must not combine HMAC (HS*) with asymmetric algorithms or a public key (algorithm-confusion risk)")
```

- [ ] **Step 5: Run, expect PASS** + full CF suite green.
- [ ] **Step 6: Commit** — `fix(cloudflare): reject HMAC+asymmetric JWT config (algorithm confusion)`

---

### Task 4 (CF-lease 🔴 High): Persist lease expiry as wall-clock, not monotonic

**Files:**
- Modify: `packages/provide-uterm-cloudflare/src/provide/uterm/cloudflare/do/session_runtime/io.py` (~91-101, lease acquire/save)
- Modify: `packages/provide-uterm-cloudflare/src/provide/uterm/cloudflare/state/store.py` (~145-162, `save_lease` / restore comparison)
- Modify: `packages/provide-uterm-cloudflare/src/provide/uterm/cloudflare/do/persistence.py` (~49, `_restore_state` comparison)
- Test: CF DO/state test module (e.g. `test_store*.py`, `test_session_runtime*.py`)

**Problem:** Lease expiry is stored as a `time.monotonic()` value in SQLite and compared against a fresh `time.monotonic()` after DO restart/hibernation. Monotonic clocks restart per isolate, so a restored lease can be falsely active (operator lockout) or falsely expired. `persist_lease` already converts to wall-clock for the CF alarm — the SQLite value and restore comparison must do the same.

- [ ] **Step 1: Read** all three call sites. Confirm where `lease_expires_at` is computed (`time.monotonic() + lease_s`), where it is written, and where `_restore_state` compares it.
- [ ] **Step 2: Write a failing test** that simulates a restart by writing a lease then comparing against a wall clock advanced past expiry:

```python
async def test_restored_lease_uses_wall_clock(do_store, monkeypatch):
    # Save a lease that expires in 30s of wall time.
    await do_store.save_lease(worker_id="w1", owner="op", lease_expires_at_wall=NOW + 30)
    # Simulate a new isolate: monotonic resets; only wall clock carries forward.
    restored = await do_store.restore_state("w1")
    assert restored.lease_active is True
    # Advance wall clock past expiry; lease must read as expired.
    monkeypatch.setattr("time.time", lambda: NOW + 31)
    restored2 = await do_store.restore_state("w1")
    assert restored2.lease_active is False
```
(Adjust to the real store API discovered in Step 1.)

- [ ] **Step 3: Run, expect FAIL.**
- [ ] **Step 4: Implement.** Store wall-clock expiry. At acquire, compute `expires_wall = time.time() + lease_s` and persist THAT to SQLite (mirror what `persist_lease` does for the alarm). In `_restore_state`/`store.get_lease`, compare `float(lease_expires_at) > time.time()`. Keep monotonic only for in-memory same-isolate countdown if it is used elsewhere; the *persisted* value must be wall-clock. Audit every read of the stored column for a stray `time.monotonic()` comparison.
- [ ] **Step 5: Run, expect PASS** + full CF suite green.
- [ ] **Step 6: Commit** — `fix(cloudflare): persist hijack lease expiry as wall-clock time`

---

### Done criteria (Lane A1)
- [ ] `uv run pytest packages/provide-uterm-cloudflare/tests/ -q` green
- [ ] `uv run ruff check --fix && uv run ruff format && uv run mypy packages/provide-uterm-cloudflare/src/`
- [ ] `uv run python scripts/run_mutation_gate.py --changed-only --min-mutation-score 100` → 0 survivors on touched CF auth/lease files
- [ ] `.ci/check_cf_vendor_tree.sh` still passes (no vendor drift)
- [ ] 4 commits, one per task. Signal A1 complete so B1 can start.

### Cross-lane requests
_(record here any change you find is needed outside `packages/provide-uterm-cloudflare/`; do not make it)_
