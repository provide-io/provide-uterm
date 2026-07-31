# Python Robustness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert malformed boundary inputs into documented refusals, eliminate the VNC test thread warning, and restore the Python server's strict coverage gate.

**Architecture:** Keep fixes at parsing boundaries: validate decoded JSON container types, normalize URL parser exceptions, and make test cleanup capability-based. Each reviewed failure gets a minimal regression test.

**Tech Stack:** Python 3.11–3.14, pytest, Pydantic, Cloudflare Python runtime shim.

---

### Task 1: PAM non-object JSON

**Files:**
- Modify: `packages/provide-uterm-platform/src/provide/uterm/pty/pam_listener.py`
- Modify: `packages/provide-uterm-platform/tests/pty/test_pam_listener.py`

- [ ] **Step 1: Add the failing parametrized test**

Pass `[]`, `"text"`, `1`, `true`, and `null` to `_parse_event`; assert `None`. Add a listener-level case proving a following valid event is still handled.

```bash
uv run --package provide-uterm-platform --extra dev pytest -q packages/provide-uterm-platform/tests/pty/test_pam_listener.py --no-cov -o addopts=--import-mode=importlib
```

- [ ] **Step 2: Validate the decoded container**

After `json.loads`, return `None` with the existing warning when `not isinstance(data, dict)`. Do not broaden the handler exception boundary.

- [ ] **Step 3: Verify and commit**

Run the focused test again and commit the source/test pair.

### Task 2: Graphical endpoint parser errors and server coverage

**Files:**
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/graphical_targets.py`
- Modify: `packages/provide-uterm-server/tests/server/test_graphical_targets.py`
- Modify: `packages/provide-uterm-server/tests/test_config_schema.py`

- [ ] **Step 1: Add failing malformed IPv6 cases**

For both endpoint parsers, pass an unclosed bracketed IPv6 address and assert `GraphicalTargetError` with `INVALID`, never raw `ValueError`.

- [ ] **Step 2: Normalize parser exceptions**

Wrap `urlparse`, `hostname`, and `port` access in one `try/except ValueError` and raise the existing client-facing invalid endpoint/port error from `None`.

- [ ] **Step 3: Cover the normalized-empty team domain**

Add an `AuthConfig` test using `https://.cloudflareaccess.com/` and assert it leaves JWKS/issuer unchanged. This executes `config_schema.py:160` without adding a coverage exclusion.

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest -q packages/provide-uterm-server/tests/server/test_graphical_targets.py packages/provide-uterm-server/tests/test_config_schema.py
git add packages/provide-uterm-server
git commit -m "fix(server): normalize malformed endpoint errors"
```

### Task 3: Cloudflare invalid JSON body

**Files:**
- Modify: `packages/provide-uterm-cloudflare/src/provide/uterm/cloudflare/do/session_runtime/io.py`
- Modify: `packages/provide-uterm-cloudflare/tests/test_session_runtime_unit_2.py`

- [ ] **Step 1: Add a failing body-decoding test**

Provide a request whose body is syntactically invalid JSON; assert `request_json` returns `{}` just like missing/non-string bodies.

- [ ] **Step 2: Catch only decode failure**

Catch `json.JSONDecodeError` around `json.loads`; retain the existing type checks and do not suppress unrelated runtime exceptions.

- [ ] **Step 3: Verify and commit**

```bash
uv run pytest -q packages/provide-uterm-cloudflare/tests/test_session_runtime_unit_2.py
git add packages/provide-uterm-cloudflare
git commit -m "fix(cloudflare): reject malformed request json"
```

### Task 4: VNC thread cleanup hygiene

**Files:**
- Modify: `packages/provide-uterm/tests/test_vnc_human_relay_driver.py`

- [ ] **Step 1: Reproduce the warning as an error**

```bash
uv run pytest -q packages/provide-uterm/tests/test_vnc_human_relay_driver.py -W error::pytest.PytestUnhandledThreadExceptionWarning
```

Expected: failure because `_FailAfter` lacks `close`.

- [ ] **Step 2: Make cleanup capability-based**

Retrieve `close = getattr(value, "close", None)` and call it only when callable, suppressing ordinary cleanup exceptions inside the background thread.

- [ ] **Step 3: Verify Python gates and tracker**

```bash
uv run pytest -q packages/provide-uterm/tests/test_vnc_human_relay_driver.py -W error::pytest.PytestUnhandledThreadExceptionWarning
uv run pytest -q packages/provide-uterm-server/tests/
uv run ruff check packages/provide-uterm-platform packages/provide-uterm-server packages/provide-uterm-cloudflare packages/provide-uterm
```

Record evidence, complete the `PY-*` tracker entries, and commit the tracker update.
