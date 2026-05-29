# Lane A3 — Platform / Manager Hardening Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Read `00-ORCHESTRATION.md` "Global constraints" first.

**Goal:** Make the fleet-manager and PTY tier fail-closed: mandatory spawn sandbox, authenticated spawn-policy webhooks, safe CORS, deterministic fork-child termination, bounded queues.

**Scope (exclusive write ownership):** `packages/provide-uterm-platform/**` only.

**Tech Stack:** Python, FastAPI, FastMCP, ctypes/PAM, os.fork/execve, pytest.

**Order:** CB-4 → PLAT-hmac → PLAT-cors → PLAT-fork → PLAT-reg → PLAT-cap.

---

### Task 1 (CB-4 🟠 High): Make the spawn config-path sandbox mandatory & symlink-safe

**Files:**
- Modify: `packages/provide-uterm-platform/src/provide/uterm/manager/routes/spawn.py:64-74` (`_validate_config_path`)
- Check: `packages/provide-uterm-platform/src/provide/uterm/manager/mcp_tools.py:~123` (same validation path)
- Modify: the `ManagerConfig` module to supply a default config dir
- Test: `packages/provide-uterm-platform/tests/` manager test module

**Problem:** `_validate_config_path` only enforces directory containment when `UTERM_CONFIG_DIR` is set — **unset by default**, so any authenticated `/swarm/spawn` caller can point at an arbitrary `.yaml` anywhere. Also `Path(config_path).resolve()` follows symlinks, so a symlinked leaf inside the dir can still escape.

- [ ] **Step 1: Read** `_validate_config_path`, the `/swarm/spawn` route, and how `ManagerConfig` exposes a config/base dir.
- [ ] **Step 2: Write failing tests:**

```python
import pytest
def test_validate_config_path_requires_base_dir(monkeypatch):
    monkeypatch.delenv("UTERM_CONFIG_DIR", raising=False)
    with pytest.raises(ValueError, match="config dir"):
        _validate_config_path("/etc/passwd.yaml")  # no base configured anywhere

def test_validate_config_path_blocks_symlink_escape(tmp_path):
    base = tmp_path / "cfgs"; base.mkdir()
    outside = tmp_path / "secret.yaml"; outside.write_text("x")
    (base / "link.yaml").symlink_to(outside)
    with pytest.raises(ValueError):
        _validate_config_path(str(base / "link.yaml"), config_dir_env=str(base))
```

- [ ] **Step 3: Run, expect FAIL.**
- [ ] **Step 4: Implement.** Resolve base from `UTERM_CONFIG_DIR` *or* `ManagerConfig`'s config dir; if neither is set, **raise** (no free traversal). Use `os.path.realpath` on both the candidate and base and require containment AFTER realpath so symlinked leaves are caught:

```python
def _validate_config_path(config_path: str, *, config_dir_env: str = "") -> Path:
    base_raw = config_dir_env or os.environ.get("UTERM_CONFIG_DIR", "").strip() or _manager_config_dir()
    if not base_raw:
        raise ValueError("config dir is not configured; refusing to spawn from an unrestricted path")
    base = Path(os.path.realpath(base_raw))
    resolved = Path(os.path.realpath(config_path))
    if resolved.suffix.lower() not in (".yaml", ".yml"):
        raise ValueError(f"config_path must be a .yaml or .yml file: {config_path}")
    if not resolved.is_relative_to(base):
        raise ValueError(f"config_path is outside config dir ({base}): {config_path}")
    return resolved
```
Add `_manager_config_dir()` reading from `ManagerConfig` (no hardcoded path inline). Update `mcp_tools.py` to call the same validator.

- [ ] **Step 5: Run, expect PASS** + `uv run pytest packages/provide-uterm-platform/tests/ -q`.
- [ ] **Step 6: Commit** — `fix(platform): make manager spawn config-path sandbox mandatory and symlink-safe`

---

### Task 2 (PLAT-hmac 🟠 High): Sign spawn-policy webhook requests

**Files:** Modify `packages/provide-uterm-platform/src/provide/uterm/manager/ext.py:43-57` (`WebhookAgentSpawnPolicyGate.intercept_spawn`). Test: manager test module.

**Problem:** The class stores `secret` but the body comment says *"In a real implementation we would add HMAC signatures here if secret is set"* — the secret is never used, so a configured spawn-authorization webhook receives unsigned requests (false security).

- [ ] **Step 1: Read** `intercept_spawn` (~43-57).
- [ ] **Step 2: Write failing test:**

```python
import hashlib, hmac, json
async def test_spawn_webhook_signs_body_when_secret_set(respx_mock):
    gate = WebhookAgentSpawnPolicyGate(url="https://policy.example/allow", secret="s3cret")
    route = respx_mock.post("https://policy.example/allow").respond(200, json={"allow": True})
    await gate.intercept_spawn("a1", "/cfg/a.yaml", {"k": "v"})
    sig = route.calls.last.request.headers["X-Signature"]
    body = route.calls.last.request.content
    expected = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(sig, expected)
```

- [ ] **Step 3: Run, expect FAIL.**
- [ ] **Step 4: Implement.** Serialize the body once, sign it, send the header:

```python
import hashlib, hmac, json
body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
headers = {"Content-Type": "application/json"}
if self.secret:
    digest = hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    headers["X-Signature"] = f"sha256={digest}"
resp = await client.post(self.url, content=body, headers=headers)
```
Remove the misleading comment. (If `secret` is meant to be required, raise on empty — confirm intended config semantics.)

- [ ] **Step 5: Run, expect PASS** + suite green.
- [ ] **Step 6: Commit** — `feat(platform): HMAC-sign spawn-policy webhook requests`

---

### Task 3 (PLAT-cors 🟠 High): Refuse `*` origins with credentials

**Files:** Modify `packages/provide-uterm-platform/src/provide/uterm/manager/app.py:88-96`. Test: manager app test module.

**Problem:** `CORSMiddleware(allow_credentials=True, ...)` with config/env-driven origins; setting `UTERM_CORS_ORIGINS=*` enables credentialed cross-site requests against a process-kill/spawn API.

- [ ] **Step 1: Read** the CORS wiring (~88-96) and where origins are read.
- [ ] **Step 2: Write failing test:**

```python
import pytest
def test_app_rejects_wildcard_origin_with_credentials(monkeypatch):
    monkeypatch.setenv("UTERM_CORS_ORIGINS", "*")
    with pytest.raises(ValueError, match="wildcard"):
        create_manager_app(...)  # factory
```

- [ ] **Step 3: Run, expect FAIL.**
- [ ] **Step 4: Implement.** When credentials are allowed, reject a `*` (or empty→`*`) origin list at app-build time with a clear `ValueError`; require an explicit origin allowlist. Log a hard warning if origins look permissive.
- [ ] **Step 5: Run, expect PASS** + suite green.
- [ ] **Step 6: Commit** — `fix(platform): refuse wildcard CORS origin when credentials are enabled`

---

### Task 4 (PLAT-fork 🟡): Catch-all before `os._exit` in the PTY fork child

**Files:** Modify `packages/provide-uterm-platform/src/provide/uterm/pty/connector.py:185-192` (post-fork child block). Test: PTY test module (may need root/native guards — gate with the existing skip markers if so).

**Problem:** Between `os.fork()` and `os.execve()`, the privilege-drop calls (`setgid`/`initgroups`/`setuid`) and `execve` are not wrapped in a catch-all. A raise from `setgid`/`setuid` (or `execve` on a nonexistent command) can unwind into inherited parent `atexit`/buffered-IO handlers in the child.

- [ ] **Step 1: Read** the child block (~185-192) and confirm the current `os._exit(127)` only covers the execve path.
- [ ] **Step 2: Write failing test** (where feasible without root): a connector whose resolved command does not exist must terminate the child with exit 127 and not raise/flush in-child. If a full fork test is infeasible in CI, add a unit test around an extracted `_child_exec(...)` helper asserting it calls `os._exit(127)` on any exception.
- [ ] **Step 3: Run, expect FAIL.**
- [ ] **Step 4: Implement.** Wrap the entire post-fork child body:

```python
# child
try:
    os.setgid(gid)
    os.initgroups(username, gid)
    os.setuid(uid)
    os.execve(cmd_path, argv, child_env)
except BaseException:  # noqa: BLE001 — child must never unwind into parent handlers
    os._exit(127)
```
Extracting `_child_exec(...)` makes it unit-testable and keeps the catch-all in one place.
- [ ] **Step 5: Run, expect PASS** + suite green.
- [ ] **Step 6: Commit** — `fix(platform): terminate PTY fork child deterministically on any setup failure`

---

### Task 5 (PLAT-reg 🟡): Cap auto-created agent records by `max_agents`

**Files:** Modify `packages/provide-uterm-platform/src/provide/uterm/manager/routes/agent_update.py:115-129` and `manager/routes/agent_ops.py:~206` (`register_agent`). Test: manager test module.

**Problem:** `POST /agent/{id}/status` and `register_agent` auto-create records for any token holder with no `max_agents` cap (only `spawn_agent` checks it) → unbounded dict/state-file growth.

- [ ] **Step 1: Read** both creation paths and how `spawn_agent` enforces `max_agents`.
- [ ] **Step 2: Write failing test:** with `max_agents=2` and 2 existing agents, a status/register for a new unknown id is rejected (HTTP 4xx / `ValueError`), not auto-created.
- [ ] **Step 3: Run, expect FAIL.**
- [ ] **Step 4: Implement.** Before auto-creating, check `len(agents) < config.max_agents`; reject with a 429/409 (or the manager's standard error) when full. Apply to both paths.
- [ ] **Step 5: Run, expect PASS** + suite green.
- [ ] **Step 6: Commit** — `fix(platform): cap auto-created agent records at max_agents`

---

### Task 6 (PLAT-cap 🟢): Bound the capture-socket queue

**Files:** Modify `packages/provide-uterm-platform/src/provide/uterm/pty/capture.py:38` (`_queue`). Test: capture test module.

**Problem:** `CaptureSocket._queue` is an unbounded `asyncio.Queue`; a fast/local client on the capture socket can OOM with no backpressure.

- [ ] **Step 1: Read** the queue creation and producer/consumer.
- [ ] **Step 2: Write failing test:** producing past a configured `maxsize` either applies backpressure or drops-oldest with a logged warning (assert the chosen policy; queue does not grow unbounded).
- [ ] **Step 3: Run, expect FAIL.**
- [ ] **Step 4: Implement.** Give the queue a bounded `maxsize` (default in the package config/constants, not hardcoded inline). On full, prefer drop-oldest + `perr`-log a "capture backpressure" warning rather than blocking the reader. Also set restrictive perms (0700 dir / 0600 socket) on the listening socket if not already.
- [ ] **Step 5: Run, expect PASS** + suite green.
- [ ] **Step 6: Commit** — `fix(platform): bound capture-socket queue to prevent OOM`

---

### Done criteria (Lane A3)
- [ ] `uv run pytest packages/provide-uterm-platform/tests/ -q` green (PAM/root/native tests may skip — that is expected)
- [ ] `uv run ruff check --fix && uv run ruff format && uv run mypy packages/provide-uterm-platform/src/`
- [ ] 6 commits, one per task.

### Cross-lane requests
_(record any out-of-scope change needed here)_
