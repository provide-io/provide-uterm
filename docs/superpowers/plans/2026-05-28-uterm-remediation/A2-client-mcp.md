# Lane A2 — Client / MCP Input-Hardening Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Read `00-ORCHESTRATION.md` "Global constraints" first.

**Goal:** Stop LLM/MCP-supplied input from reaching a live terminal or backend unsanitized.

**Scope (exclusive write ownership):** `packages/provide-uterm-client/**` only.

**Tech Stack:** Python, FastMCP, httpx, pytest.

**Order:** MCP-send (🟠 High) → MCP-host (🟡) → MCP-redos (🟡).

---

## Tasks

### Task 1 (MCP-send 🟠 High): Sanitize `hijack_send` keystrokes

**Files:**
- Modify: `packages/provide-uterm-client/src/provide/uterm/ai/server_impl.py:311-327` (`hijack_send`) and `_unescape_keys` (~60-90)
- Reuse: `packages/provide-uterm-client/src/provide/uterm/client/sanitizer.py` (`sanitize_keystrokes`)
- Test: `packages/provide-uterm-client/tests/ai/` (locate the MCP tool test module)

**Problem:** The wired MCP `hijack_send` tool applies only `_unescape_keys()` (which *expands* `\xNN`/`\uNNNN`/`\e` into arbitrary control bytes) and does NO filtering, while the sibling `client/mcp_tools.py:112` path runs `sanitize_keystrokes()` (allowlist + 4096-byte cap). An LLM can inject full ANSI/OSC sequences into a hijacked terminal.

**Existing helper (already correct):** `sanitize_keystrokes(keys, max_bytes=4096)` allows printable + `\r \n \t \x03 \x1b` and caps bytes.

- [ ] **Step 1: Read** `server_impl.py` lines ~60-90 (`_unescape_keys`, `_ESCAPE_PATTERN`) and ~311-327 (`hijack_send`). Note `sanitize_keystrokes` is importable from `provide.uterm.client.sanitizer`.

- [ ] **Step 2: Write failing test** in the MCP tool test module:

```python
async def test_hijack_send_strips_injected_control_sequences(mcp_client_stub):
    # An OSC sequence (\x1b]0;pwned\x07) and a NUL must not survive to the wire.
    sent = {}
    mcp_client_stub.capture("hijack_send", into=sent)
    await call_tool("hijack_send", keys="ls\\x00\\x1b]0;pwned\\x07rm -rf /")
    wire = sent["keys"]
    assert "\x00" not in wire            # NUL filtered
    assert "\x07" not in wire            # BEL (OSC terminator) filtered
    assert "]0;pwned" not in wire        # OSC body cannot drive terminal title/clipboard
    assert len(wire.encode("utf-8")) <= 4096
```
(`\x1b` ESC and `\x03` Ctrl-C are intentionally still allowed; assert ESC alone does not carry an OSC payload through.)

- [ ] **Step 3: Run, expect FAIL.** `uv run pytest packages/provide-uterm-client/tests/ai/ -k hijack_send -v`

- [ ] **Step 4: Implement.** In `hijack_send`, sanitize AFTER unescaping:

```python
from provide.uterm.client.sanitizer import sanitize_keystrokes
...
result = await client.hijack_send(
    keys=sanitize_keystrokes(_unescape_keys(keys)),
    ...
    expect_regex=expect_regex,
)
```
Order matters: `_unescape_keys` first (so `\x1b` text → ESC byte), then `sanitize_keystrokes` (so the now-real control bytes are filtered to the allowlist + capped). Verify `client/mcp_tools.py:112` already does this; if the two diverge in any other way, unify on a single shared helper so they cannot drift again.

- [ ] **Step 5: Run, expect PASS** + `uv run pytest packages/provide-uterm-client/tests/ -q` green.
- [ ] **Step 6: Commit** — `fix(client): sanitize hijack_send keystrokes after unescaping`

---

### Task 2 (MCP-host 🟡): Validate `host` in `session_create`

**Files:** Modify `packages/provide-uterm-client/src/provide/uterm/ai/server_impl.py:~169-176` (`_validate_session_create_config`) and `~438-441` (`session_create`). Test: same dir.

**Problem:** The URL-scheme allowlist blocks `file://`/`javascript:`, but a raw `host`+`port` for `telnet`/`ssh`/`ws` is forwarded straight to `quick_connect` → `/api/connect` with no validation — an SSRF/internal-pivot primitive (`169.254.169.254`, `localhost`, RFC1918) driven by model input. (Admin-gated, hence Medium.)

- [ ] **Step 1: Read** `_validate_session_create_config` to see how scheme/port are already checked.
- [ ] **Step 2: Write failing test:**

```python
import pytest
@pytest.mark.parametrize("host", ["169.254.169.254", "127.0.0.1", "localhost", "10.0.0.5", "::1", "metadata.google.internal"])
async def test_session_create_rejects_internal_hosts(host):
    with pytest.raises(ValueError, match="host"):
        _validate_session_create_config({"connector": "telnet", "host": host, "port": 23})
```

- [ ] **Step 3: Run, expect FAIL.**
- [ ] **Step 4: Implement.** Add a `host` validator: resolve/normalize and reject link-local (`169.254.0.0/16`, `fe80::/10`), loopback (`127.0.0.0/8`, `::1`), and—**behind a config flag defaulting to deny**—RFC1918 / unique-local. Use `ipaddress` for literals; for hostnames, reject the known metadata names and document that DNS-rebinding/egress filtering remains the server's responsibility (do NOT do a blocking DNS lookup in the validator). Put the allow/deny toggle in the package's existing config module (no hardcoded policy inline). Wire it into `_validate_session_create_config` before `quick_connect`.
- [ ] **Step 5: Run, expect PASS** + suite green.
- [ ] **Step 6: Commit** — `fix(client): validate session_create host against internal/metadata targets`

---

### Task 3 (MCP-redos 🟡): Bound attacker-supplied regex

**Files:** Modify `server_impl.py:~316` (`expect_regex` param of `hijack_send`), `~533-582` (`session_subscribe`, the local `re.compile(pattern)`), and the `session_watch`/`pattern` path. Test: same dir.

**Problem:** `expect_regex`/`pattern` are passed to `re.compile` (server- and client-side) with no length or complexity limit; `session_subscribe` is only viewer-gated. A catastrophic-backtracking pattern pins CPU (ReDoS).

- [ ] **Step 1: Read** `session_subscribe` (~533-582) and `hijack_send` (~311-331) to find every `re.compile` / `expect_regex` pass-through.
- [ ] **Step 2: Write failing test:**

```python
import pytest
async def test_session_subscribe_rejects_oversized_pattern():
    with pytest.raises(ValueError, match="pattern"):
        await call_tool("session_subscribe", session_id="s1", pattern="a" * 2000)

def test_compile_user_pattern_caps_length():
    from provide.uterm.client.ai.server_impl import _compile_user_pattern
    with pytest.raises(ValueError):
        _compile_user_pattern("x" * 2000)
```

- [ ] **Step 3: Run, expect FAIL.**
- [ ] **Step 4: Implement.** Add one shared helper used by every pattern entry point:

```python
_MAX_USER_PATTERN_LEN = 512  # define alongside other limits in the package config/constants module

def _compile_user_pattern(pattern: str) -> "re.Pattern[str]":
    if len(pattern) > _MAX_USER_PATTERN_LEN:
        raise ValueError(f"pattern too long (max {_MAX_USER_PATTERN_LEN} chars)")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid pattern: {exc}") from exc
```
Route `session_subscribe`'s local compile and any client-side compile through it. For `expect_regex` forwarded to the server, apply the length cap before sending and document that the *server* must also bound matching time (raise that as a cross-lane request to A4 if the server compiles it). Note: true backtracking-time bounds need `regex`/`re2`; the length cap is the pragmatic mitigation here — note the residual risk in the docstring.

- [ ] **Step 5: Run, expect PASS** + suite green.
- [ ] **Step 6: Commit** — `fix(client): cap length of user-supplied match patterns (ReDoS)`

---

### Done criteria (Lane A2)
- [ ] `uv run pytest packages/provide-uterm-client/tests/ -q` green
- [ ] `uv run ruff check --fix && uv run ruff format && uv run mypy packages/provide-uterm-client/src/`
- [ ] 3 commits, one per task.

### Cross-lane requests
- Likely: ask **A4 (server)** to bound server-side regex matching time for `expect_regex`/`pattern` (record exact endpoint here once found).
