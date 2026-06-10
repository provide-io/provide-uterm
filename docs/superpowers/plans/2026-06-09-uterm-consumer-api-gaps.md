# uterm Consumer-Driven API Gaps — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Use `superpowers:test-driven-development` for every code task — this repo enforces 100% branch coverage, so tests come first, always.

**Goal:** Close the API gaps in `provide-uterm` that force its largest real consumer (`uwarp-space`) to reimplement, work around, or under-use uterm features — then (optionally, Part B) update `uwarp-space` to adopt the cleaned-up surface.

**Implementation status (2026-06-09):** Part A is complete on `main` through commit `d5602c0b` (`Implement uterm consumer API gaps`), building on earlier merged subagent commits `68750619` (U6) and `af8b7fa8` (U7/U8 and subagent branch merges). Verified with `make quality-gate`, `uv run python scripts/run_all_tests.py`, and the changed-only mutation gate (`mutation_score=100.00`, with one documented equivalent in `mutation_equivalents.toml`). Part B downstream work has been merged to `uwarp-space/main` through commit `f7ce8e053`: public API adoption, B-F10-session, B-F8 detector extraction, runtime prompt FlowEngine, and TWGS character-login FlowEngine are landed. Remaining active B-F9 work: TWGS pre-character routing and direct uwarp `login.py` migration.

**Handoff notes:** this plan is intentionally in `docs/superpowers/plans/` and assumes a subagentized execution. Keep Part A and Part B split by repository boundaries; if another LLM only owns uterm work, copy §6 into a separate `uwarp` handoff file and leave this doc as the uterm source of truth.

**Architecture:** This is driven by an audit of how `uwarp-space` consumes uterm. The audit found uwarp uses uterm *well* at the architectural seams (transports, gateways, the bridge hub, the pyte emulator) but **under-adopts uterm's higher-level value-add** and re-derives logic uterm already owns. The fix is split into **Part A — uterm changes** (8 work items, `U1`–`U8`, the real engineering) and **Part B — uwarp adoption** (13 findings, `F1`–`F13`, downstream, gated on Part A). Part A is the priority; Part B is a separate repo and can be handed to a different worker once Part A lands.

**Subagent map:** when decomposing:
- Subagent 1: U6/U7/U8 + U1a + cross-reference matrix/ordering updates.
- Subagent 2: U4 + U5 + related schema/codegen verification.
- Subagent 3: U1b + U2 + U3 (keep `rules.py` flow internals read-first for both).
- Subagent 4: Tier B1 in `uwarp-space` while Part A advances (no dependency).
- Subagent 5: Tier B2/B3 as each gate unlocks.

**Worktree execution status:**
- `/Volumes/data/pyv/provide-uterm` (main): Part A implementation complete and committed.
- API-gap subagent worktrees/branches (`agent-u1a`, `agent-u2-u3`, `agent-u4-u5`, `agent-u6-uws`, `agent-u7-u1`, `full-uterm-migration`) were merged into `main`, verified to have no unique commits, removed, and pruned.
- `.claude/worktrees/agent-aa59...` (`upgrade/python-latest`) and `.claude/worktrees/agent-af3...` (`upgrade/js-docker`) are unrelated dependency-upgrade worktrees with unique commits and remain isolated for the next phase.

**Tech Stack:** Python ≥3.11, uv workspace, pytest (`asyncio_mode=auto`), Pydantic v2 (wire frames), `websockets`, `asyncssh`, pyte (terminal emulation), ruff + mypy strict + bandit. Mutation testing (mutmut). TypeScript frontend consumes generated frame schemas.

---

## 0. How to use this document

You (the receiving LLM/engineer) have **zero prior context**. Read sections 1–4 fully before touching code — they contain the repo topology, the hard constraints (CI gates that will fail your PR), and the already-verified ground-truth facts so you don't redo or distrust them.

- **Part A** (§5) is the uterm work. Each task lists exact files, an interface contract, test cases, and acceptance criteria. Do them in the order in §7.
- **Part B** (§6) is the uwarp work. It lives in a **different repo** and most items are *gated* on Part A landing + being published. Do not start Part B items whose uterm dependency hasn't shipped.
- **§8** is the cross-reference matrix (Finding ↔ uterm change ↔ uwarp fix). **§9** is sequencing.

Each item is tagged **blocker** (uwarp literally cannot adopt the clean path without it) or **enabler** (uwarp could work around it, but the change removes boilerplate or a footgun).

---

## 1. Repo topology & ground truth

### Two repos, one editable dependency

| Repo | Path | Role |
|---|---|---|
| **uterm** (this repo) | `/Volumes/data/pyv/provide-uterm` (also symlinked at `/Users/tim/code/gh/provide-io/provide-uterm` — `realpath` confirms identical) | The terminal library. **All Part A work happens here.** |
| **uwarp-space** | `/Users/tim/code/gh/undef-games/uwarp-space` | A TradeWars 2002 (TW2002) game platform. The consumer being audited. **Part B work happens here.** |

uwarp depends on uterm via **editable path installs** (no fork, no vendoring) — `uwarp-space/pyproject.toml:199-201`:
```toml
provide-uterm        = { path = "../../provide-io/provide-uterm/packages/provide-uterm",        editable = true }
provide-uterm-client = { path = "../../provide-io/provide-uterm/packages/provide-uterm-client", editable = true }
provide-uterm-server = { path = "../../provide-io/provide-uterm/packages/provide-uterm-server", editable = true }
```
So changes you make in this repo are immediately live in uwarp's venv. Good for Part B testing; means you must not break uwarp's import surface without coordinating.

### uterm package layout (the parts this plan touches)

```
packages/provide-uterm/src/provide/uterm/          # CORE
  ansi.py                  # color/ANSI: normalize_colors, upgrade_to_256/truecolor, DEFAULT_PALETTE
  control_channel.py       # encode_control:93, ControlChannelDecoder:121, DLE-STX header:44-46,225-240
  control_channel_builders.py  # make_identity/session_token/resume/resume_ok/resume_failed/link_patterns/presence_update
  detection/               # rules.py, engine.py, detector.py, extractor.py, models.py
  deckmux/                 # __init__.py (public), _hub_mixin.py (PRIVATE — DeckMuxMixin lives here)
  transport_session.py     # TransportSession base: connect, add_watch:192, wait_for_update/screen_change:134-188, __aenter__/__aexit__:107-112
  telnet_session.py        # connect_telnet:43, class TelnetSession(TransportSession):73
  ws_session.py            # connect_ws:31, class WebSocketSession
  session_logger.py        # SessionLogger:28, nested LegacyFileStore:54-81 (hardened secure-open is TRAPPED here)
  file_io.py               # existing file helpers — target for the extracted secure-open
  bridge/schemas.py        # Pydantic wire frames + AnyFrame union (ResumeFrame:222, SnapshotFrame:68)

packages/provide-uterm-client/src/provide/uterm/
  transports/ws_transport.py   # WebSocketTransport.connect:37 — calls websockets.connect(self._url) BARE at :51
  transports/websocket.py      # WebSocketStreamWriter/Reader
  client/sanitizer.py          # prepare_keystrokes:76 — STANDALONE, zero coupling (good)
  client/hijack.py             # HijackClient:72 — HTTP-to-hub; expect_regex forwarded server-side at send():231-256
  client/mcp_tools.py          # hijack_tools (the "21 MCP tools")

packages/provide-uterm-server/src/provide/uterm/server/
  bridge/frames.py             # make_snapshot_frame:196, make_term_frame:189, ... (return Pydantic models)
  bridge/hub/                  # TermHub (9 services)
  bridge/routes/websockets.py  # register_ws_routes
  bridge/routes/websockets_browser.py  # feature-detects deckmux_handle_message at :89
```

### uwarp layout (for Part B)

uwarp is **two halves**:
- **Server reimplementation** — `packages/uwarp-worker/`, `packages/uwarp-server/`, `packages/uwarp/`. This is uwarp's *own* TW2002 server; it generates structured prompt events and does NOT screen-scrape. uterm's detection is irrelevant here.
- **Bot/client (explorer)** — `packages/uwarp-explorer/`. Drives an *external* TWGS/TW2002 server over telnet and must screen-scrape. **This is where almost all the detection/session findings live.**

> ⚠️ `worker/src/` in uwarp is **generated** from `packages/uwarp-worker/src/uwarp_worker/` by `worker/scripts/sync_uwarp.py` and is **gitignored**. Edit `packages/`, never `worker/src/`. The `packages/` copy is canonical.

---

## 2. Hard constraints (CI gates that WILL fail your PR)

These come from `CLAUDE.md` in this repo and the user's global standards. Violating any of them fails CI or pre-commit:

1. **100% branch + line coverage** (`--cov-fail-under=100`). Every new line/branch needs a test. This is why every task below is TDD.
2. **Mutation testing** — `killed==100` on the curated perimeter (security-critical surfaces, hub services, frame schemas, `manager/process_impl`). If you split a perimeter file for the 500-LOC limit, add the extracted sibling to `[tool.mutmut] paths_to_mutate` in root `pyproject.toml`. See `MUTATION_PATTERNS.md`.
3. **New wire frames require codegen.** Frame models live ONLY in `packages/provide-uterm/src/provide/uterm/bridge/schemas.py`. After adding/changing a model: run `uv run python scripts/codegen_frames.py`, then commit `schemas.py`, `frames.schema.json`, and `frontend/src/generated/frames.ts` together. Pre-commit + CI run `scripts/codegen_frames.py --check` to catch drift. **U4 touches frames — obey this.**
4. **SPDX headers on all new `.py` files**: the two-line header
   ```python
   # SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
   # SPDX-License-Identifier: AGPL-3.0-or-later
   ```
   (Markdown docs are covered by the `Files: *` wildcard in `.reuse/dep5` — no header needed.)
5. **500-LOC max per file** (enforced in `ci/quality_checks.sh`). Split by responsibility if you approach it.
6. **No hardcoded/inline URLs or ports.** Put them in a defaults module (`defaults.py`) or at the top of the file. If a port is already in use, STOP and ask.
7. **No inline scripts in workflow YAML** (>3 lines → extract to `ci/`). Not expected to matter here, but noted.
8. **Logging, not print.** Use the project's logging (`pout`/`perr` for debugging). PTH: use `Path.cwd()` not `os.getcwd()`.
9. **Commit cadence:** one logical unit per commit; never batch end-of-session polish into a single commit. Do not mention AI assistance in commit messages. Do not do git rollbacks (changes are auto-committed).
10. **Static gate before pushing:** `make quality-gate` (== `bash ci/quality_checks.sh`) runs the exact CI `quality` job (max-LOC, SPDX, codegen drift, ruff, mypy/ty, bandit, xenon, vulture, pip-audit, licenses). Run it locally.

### Verification commands (memorize these)

```bash
# Full core + cloudflare suite, 100% coverage enforced (the root gate):
uv run pytest

# Every workspace package's Python tests with its own coverage config:
uv run python scripts/run_all_tests.py        # <-- the REAL gate; use this, not per-file pytest

# A single test:
uv run pytest packages/provide-uterm/tests/path/test_x.py::test_name -vv

# Static quality gate (CI parity for the `quality` job):
make quality-gate

# Type check / lint / format:
uv run mypy packages/provide-uterm/src/ ; uv run ty check packages/provide-uterm/src/
uv run ruff check --fix ; uv run ruff format

# Frame codegen (after touching schemas.py):
uv run python scripts/codegen_frames.py            # regenerate
uv run python scripts/codegen_frames.py --check     # CI drift check

# Mutation gate (changed-only):
uv run python scripts/run_mutation_gate.py --changed-only
```

> ⚠️ **Never run a foreground `uv` command while a background gate is running** — the `.venv` race causes exit code 2. (Recorded operational hazard.)

---

## 3. Already-verified facts (do NOT redo or distrust these)

These were established by reading the actual source. Each is a load-bearing premise for a task.

| Fact | Evidence | Used by |
|---|---|---|
| `add_watch` IS on the `TransportSession` base → both `TelnetSession` and `WebSocketSession` have it. | `transport_session.py:192`; `telnet_session.py:73` subclasses base | U8, F4, F5 |
| The handshake/control builders ALL exist and ARE exported from core `__init__.__all__`: `make_identity/session_token/resume/resume_ok/resume_failed/link_patterns/presence_update`. | `control_channel_builders.py:41,91,112,133,142,188,210`; `__init__.py:56-62,124-130` | U4, F1 |
| The **snapshot** builder lives in the SERVER package and returns a Pydantic model, not a dict — a second, inconsistent builder family. | `server/bridge/frames.py:196` (`make_snapshot_frame`) | U4, F1 |
| `prepare_keystrokes` is STANDALONE (imports only `re`, `string`) — usable on any session today, no uterm change needed. | `client/sanitizer.py:76` | F10 (sanitizer half) |
| The guarded-send `expect_text`/`expect_regex`/`timeout_ms` semantics exist ONLY server-side behind `HijackClient`'s HTTP API — the client merely forwards `expect_regex` in the request body. No session-level reusable primitive. | `client/hijack.py:231-256` | U2, F10 |
| `RuleSet` models `flows: list[FlowRule]` and `menus: list[MenuRule]`, but `to_prompt_patterns()` iterates `self.prompts` ONLY, and NO code in `detection/` reads `.flows`/`.menus`. They are parsed-then-discarded. | `detection/rules.py:84,109,115,119,120,123-150`; grep of `detection/` for `.flows`/`.menus`/`FlowRule` consumers = empty | U3, F8, F9 |
| WS transport has NO keepalive/reconnect: `WebSocketTransport.connect` calls `websockets.connect(self._url)` with **no kwargs forwarded** (`**kwargs` is accepted but only `kwargs.get("url")` is read), and `connect_ws` exposes only `cols`/`rows`. | `ws_transport.py:37,48,51`; `ws_session.py:31-35` | U1, F7 |
| `SessionLogger`'s hardened atomic-0o600 / 0o700-parent / no-chmod-after-open file creation is trapped in a `LegacyFileStore` class defined INSIDE `SessionLogger.__init__`; the redactor is a ctor arg. Not independently importable. | `session_logger.py:28,41,54-81,127` | U5, F11 |
| `DeckMuxMixin` lives in underscore-private `deckmux/_hub_mixin.py` and is NOT in `deckmux/__init__.__all__`, yet it's the *intended* extension point (router feature-detects `deckmux_handle_message`). | `deckmux/__init__.py:26` (`__all__` without it); `_hub_mixin.py:5-25`; `server/bridge/routes/websockets_browser.py:89` | U6, F12 |
| uwarp's `tools/ansi.py` is a verbatim fork of uterm's color-upgrade engine and uwarp already imports the real one elsewhere. | uwarp `tools/ansi.py:25-216` vs uterm `ansi.py:30,434,450`; uwarp `screens.py:17` imports `upgrade_to_truecolor` from uterm | F2 |
| The F5 poll fallback is DEAD code, not active data loss — the `getattr(session, "add_watch", None)` guard takes the correct watch path; only the comment is wrong and the branch is unreachable. | uwarp `worker_term_bridge.py:82-86` | F5 (don't overstate it) |

---

## 4. The audit findings (motivation for every task)

Full finding list, tagged by category. Part A tasks reference these by ID. (Evidence paths in the F-list are in the **uwarp** repo.)

**Reimplementation (uterm already does it):**
- **F1** — Control frames (`resume`, `session_token`, `resume_ok/failed`, `link_patterns`, `snapshot`) hand-built as raw dicts; uwarp imports `control_channel_builders` zero times. `_ts_bridge.py:115`, `_ws_protocol.py:113,119,160,168`, `_control_frames.py:141`, `worker_term_bridge.py:241`.
- **F2** — `tools/ansi.py:25-216` is a verbatim fork of uterm's color-upgrade engine.
- **F3** — `_ws_protocol.py:62-84` re-parses the DLE-STX framing header by hand.
- **F4** — `_sector_fighter_helpers.py:20-57` monkeypatches `_emulator.process` to tee raw bytes (`add_watch` does this).

**Workaround (bad):**
- **F5** — `worker_term_bridge.py:72-86,108-146` dead snapshot-poll fallback + stale comment claiming `TelnetSession` lacks `add_watch`.
- **F6** — `io/helpers.py:219-239` `_wait_for`/`_wait_any` busy-poll on a fixed 200 ms sleep (sibling `_send` already uses `wait_for_update`).

**Workaround (legitimate — DO NOT "fix"):** reconnect/relogin loops (uterm has no reconnect), the Pyodide `core/text.py` grammar mirror (drift-tested), CR/LF-preserving `strip_csi` parsers, the `cf_transport.py` copy (bundler constraint), the bespoke admin JWT.

**Misleading docs:**
- **F7** — comments label uterm's session "resilient/auto-reconnecting"; it isn't. `case_library_runner_reconnect.py:36-41,118-119`.

**Under-adoption:**
- **F8** — Detection engine used only in the heartbeat; only `match.prompt_id` consumed; `kv_data`/`is_idle`/`buffer` discarded and re-extracted with parallel regex. `worker_runtime.py:264-265`, `worker_runtime_execution.py:23,41-43`.
- **F9** — Login/character-creation is a hand-rolled substring state machine re-detecting prompts already in `rules.json`. `login.py:113-240`.
- **F10** — Explorer MCP session tools reinvent the hijack contract over local sessions without `HijackClient` and without `prepare_keystrokes`. `mcp/server.py:195-254`.
- **F11** — Custom compare-log JSONL writer bypasses `SessionLogger` redaction + 0o600 hardening via plain `open().write`. `compare_log/_streams.py:162-173`.
- **F12** — `DeckMuxTermHub` subclasses via private `deckmux._hub_mixin.DeckMuxMixin`. uwarp `watch.py:12,16`.
- **F13** — Bespoke HS256 admin auth leaves uterm's auth modes + `SSHKeyResolver` hook unused. `api/auth.py:26-100`. (Low priority; defensible.)

---

## PART A — uterm changes

Each task: **Files → Steps (TDD) → Acceptance.** Tasks are ordered easiest-first within §7. Tag in the heading.

> File-structure decisions locked here:
> - U1 keepalive lives in existing `ws_transport.py`/`ws_session.py`/`telnet_session.py`; the reconnect policy is a NEW file `transports/reconnect.py` (client pkg).
> - U2 `send_expect` is a NEW module `expect.py` (core) consumed by `transport_session.py` — keeps `transport_session.py` under 500 LOC.
> - U3 flow driver is a NEW file `detection/flow.py`.
> - U5 secure-open goes into existing `file_io.py`; redaction helper into a NEW `redaction.py` (core) so `session_logger.py` shrinks.
> - U7 helper goes into existing `control_channel.py`.
> - U6 is edits to `deckmux/__init__.py` only.

---

### Task U6: Promote `DeckMuxMixin` to public API — *blocker (F12)*

*Status: complete; merged into `main` and API-gap worktree removed.*

**Files:**
- Modify: `packages/provide-uterm/src/provide/uterm/deckmux/__init__.py`
- Modify (optional, preferred public name): `packages/provide-uterm/src/provide/uterm/deckmux/_hub_mixin.py` — add a public alias for `_deckmux_init`
- Test: `packages/provide-uterm/tests/deckmux/test_public_api.py`

- [x] **Step 1 — Write the failing test.**
```python
# tests/deckmux/test_public_api.py
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
from provide.uterm import deckmux


def test_deckmux_mixin_is_public():
    # It is the documented extension point; consumers must not import a _private module.
    assert "DeckMuxMixin" in deckmux.__all__
    assert hasattr(deckmux, "DeckMuxMixin")


def test_deckmux_init_has_public_name():
    from provide.uterm.deckmux import DeckMuxMixin
    # public, non-underscore init hook
    assert hasattr(DeckMuxMixin, "deckmux_init")
```

- [x] **Step 2 — Run it, verify it fails.** `uv run pytest packages/provide-uterm/tests/deckmux/test_public_api.py -vv` → FAIL (`DeckMuxMixin` not in `__all__`).

- [x] **Step 3 — Implement.** In `deckmux/__init__.py`: `from provide.uterm.deckmux._hub_mixin import DeckMuxMixin` and add `"DeckMuxMixin"` to `__all__`. In `_hub_mixin.py`, add a public method `deckmux_init` that calls/aliases the existing `_deckmux_init` (keep `_deckmux_init` as a thin backward-compat alias so nothing in-repo breaks). Update the `_hub_mixin.py` docstring to state it is now re-exported publicly.

- [x] **Step 4 — Run all deckmux tests + grep for internal users.** `uv run pytest packages/provide-uterm/tests/deckmux/ -vv`. Then `grep -rn "_deckmux_init\|_hub_mixin" packages/` and confirm in-repo callers still work (alias preserves them).

- [x] **Step 5 — Commit.** `feat(deckmux): export DeckMuxMixin and a public deckmux_init hook`

**Acceptance:** `from provide.uterm.deckmux import DeckMuxMixin` works; `__all__` includes it; existing private callers unaffected; coverage 100%.

---

### Task U7: `is_control_framed()` helper — *enabler (F3)*

*Status: complete; merged into `main` and API-gap worktree removed.*

**Files:**
- Modify: `packages/provide-uterm/src/provide/uterm/control_channel.py` (header layout is at `:44-46,225-240`)
- Test: `packages/provide-uterm/tests/test_control_channel.py` (add to existing)

- [x] **Step 1 — Write the failing test.**
```python
def test_is_control_framed_detects_dle_stx_header():
    from provide.uterm.control_channel import encode_control, is_control_framed
    framed = encode_control({"type": "resume_ok"})
    assert is_control_framed(framed) is True


def test_is_control_framed_rejects_plain_text():
    from provide.uterm.control_channel import is_control_framed
    assert is_control_framed("just terminal output\r\n") is False
    assert is_control_framed("") is False
```

- [x] **Step 2 — Run, verify fail.** `uv run pytest packages/provide-uterm/tests/test_control_channel.py -k is_control_framed -vv` → FAIL (no `is_control_framed`).

- [x] **Step 3 — Implement.** Add a pure function to `control_channel.py` that checks the message begins with the DLE-STX magic + an 8-hex-digit length + `:` separator, reusing the SAME constants the decoder uses (do NOT duplicate the magic bytes — reference the module-level constants at `control_channel.py:44-46`). Add `is_control_framed` to the module's `__all__`/exports.

- [x] **Step 4 — Run.** `uv run pytest packages/provide-uterm/tests/test_control_channel.py -vv` → PASS.

- [x] **Step 5 — Commit.** `feat(control-channel): add is_control_framed() framing-sniff helper`

**Acceptance:** Helper is exported and reuses existing header constants (no copied magic bytes). Covers framed, plain, and empty inputs.

---

### Task U1: Transport keepalive + reconnect scaffold — *enabler (F7)*

Two independent sub-tasks. U1a is a one-liner; U1b is a small new module.

#### U1a — Forward keepalive ping to `websockets.connect`

**Files:**
- Modify: `packages/provide-uterm-client/src/provide/uterm/transports/ws_transport.py:37-51`
- Modify: `packages/provide-uterm/src/provide/uterm/ws_session.py:31` (surface the params)
- Test: `packages/provide-uterm-client/tests/transports/test_ws_transport.py` (add)

- [x] **Step 1 — Failing test (mock `websockets.connect`, assert kwargs forwarded).**
```python
async def test_connect_forwards_ping_interval(monkeypatch):
    from provide.uterm.transports import ws_transport
    captured = {}

    async def fake_connect(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        class _WS:  # minimal stub
            async def close(self): ...
        return _WS()

    monkeypatch.setattr(ws_transport.websockets, "connect", fake_connect)
    t = ws_transport.WebSocketTransport()
    await t.connect("h", 1, url="wss://x", ping_interval=20, ping_timeout=20)
    assert captured["ping_interval"] == 20
    assert captured["ping_timeout"] == 20
```

- [x] **Step 2 — Run, verify fail** (currently kwargs are dropped at `ws_transport.py:51`).

- [x] **Step 3 — Implement.** In `WebSocketTransport.connect`, forward a curated allowlist of keepalive kwargs (`ping_interval`, `ping_timeout`, `close_timeout`, `max_size`) into `websockets.connect(self._url, **forwarded)`. Do NOT blindly splat all kwargs (the method also receives `url`/`host`/`port`). Add `ping_interval`/`ping_timeout` params to `connect_ws()` in `ws_session.py` and thread them to the transport via `WebSocketSession.__init__`. Defaults: define in `defaults.py` (constraint #6) — e.g. `WS_PING_INTERVAL = 20`, `WS_PING_TIMEOUT = 20`; do not inline the numbers.

- [x] **Step 4 — Run** the transport tests + `uv run pytest packages/provide-uterm/tests -k ws_session -vv`.

- [x] **Step 5 — Commit.** `feat(transport): forward websocket keepalive ping params through connect_ws`

#### U1b — Reconnect policy + reconnecting wrapper

**Files:**
- Create: `packages/provide-uterm-client/src/provide/uterm/transports/reconnect.py`
- Test: `packages/provide-uterm-client/tests/transports/test_reconnect.py`

**Interface contract:**
```python
# reconnect.py
@dataclass(frozen=True)
class ReconnectPolicy:
    max_retries: int = 5          # 0 = no reconnect
    base_backoff_s: float = 0.5
    max_backoff_s: float = 30.0
    # exponential backoff: min(max_backoff_s, base_backoff_s * 2**attempt)

OnReconnect = Callable[["TransportSession"], Awaitable[None]]  # app re-auth hook

async def connect_with_reconnect(
    connect: Callable[[], Awaitable[TransportSession]],   # e.g. partial(connect_ws, url)
    *, policy: ReconnectPolicy = ReconnectPolicy(),
    on_reconnect: OnReconnect | None = None,
) -> ReconnectingSession: ...
```
- `ReconnectingSession` wraps a live session, proxies `send`/`snapshot`/`wait_for_update`/`wait_for_screen_change`/`add_watch`, and on a dropped connection (`ConnectionError`/`ConnectionClosed`) transparently re-runs `connect()` per `policy`, then awaits `on_reconnect(new_session)` so the **app** performs relogin. uterm owns transport reconnect only; relogin stays app-side (TW2002 menu-bounce is not uterm's concern).

- [x] **Step 1 — Failing tests.** Cover: (a) success first try → no retry, `on_reconnect` not called; (b) drop then reconnect within `max_retries` → `on_reconnect` awaited once with the new session; (c) exhaust `max_retries` → raises `ConnectionError`; (d) backoff is bounded by `max_backoff_s`. Use a fake connect factory whose first N calls raise. Patch sleep to avoid real waits (e.g. monkeypatch `asyncio.sleep`).
```python
async def test_reconnects_and_calls_hook(monkeypatch):
    from provide.uterm.transports import reconnect
    calls = {"n": 0, "hook": 0}
    class _S:  # fake session
        async def send(self, d): ...
        async def close(self): ...
    async def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            return _S()
        return _S()
    async def hook(s): calls["hook"] += 1
    monkeypatch.setattr(reconnect.asyncio, "sleep", lambda *_: _noop())
    rs = await reconnect.connect_with_reconnect(factory, on_reconnect=hook)
    await rs.reconnect()  # simulate a drop-triggered reconnect
    assert calls["hook"] == 1
```
*(Adjust to the final `ReconnectingSession` surface; the executor should design the drop-detection seam test-first.)*

- [x] **Step 2-4 — TDD loop** until all cases pass + 100% branch coverage on the new file. Keep `reconnect.py` < 500 LOC.

- [x] **Step 5 — Commit.** `feat(transport): add ReconnectPolicy + connect_with_reconnect wrapper`

**Acceptance:** A consumer can do `connect_with_reconnect(partial(connect_ws, url), policy=..., on_reconnect=relogin)` and survive transport drops without writing its own loop. No relogin logic in uterm. `defaults.py` holds the backoff numbers.

---

### Task U5: Extract `SessionLogger` secure-open + redaction — *blocker (F11)*

**Files:**
- Modify: `packages/provide-uterm/src/provide/uterm/file_io.py` (add `secure_create`, `secure_open_append`)
- Create: `packages/provide-uterm/src/provide/uterm/redaction.py` (extract the redaction filter)
- Modify: `packages/provide-uterm/src/provide/uterm/session_logger.py` (consume the extracted helpers; remove the nested `LegacyFileStore` hardening duplication at `:54-81`, `:127`)
- Test: `packages/provide-uterm/tests/test_file_io_secure.py`, `packages/provide-uterm/tests/test_redaction.py`

**Interface contract:**
```python
# file_io.py — atomic, owner-only, symlink-refusing creation (lift the logic from session_logger.py:54-81)
def secure_create(path: Path, *, mode: int = 0o600, dir_mode: int = 0o700) -> int: ...      # returns fd
def secure_open_append(path: Path, *, mode: int = 0o600, dir_mode: int = 0o700) -> TextIO: ...
#   - parent dirs created with dir_mode
#   - O_CREAT|O_WRONLY|O_APPEND|O_NOFOLLOW, mode at open time (NO chmod-after-open)

# redaction.py
def make_redactor(patterns: Sequence[str] | None = None) -> Callable[[str], str]: ...
def redact_text(text: str, redactor: Callable[[str], str] | None) -> str: ...
```

- [x] **Step 1 — Failing tests for `file_io`:** file created with mode `0o600`; parent dir `0o700`; opening a path that is a symlink raises (O_NOFOLLOW); append doesn't truncate. (On platforms without `O_NOFOLLOW`, skip with a marker — but macOS/Linux have it.)
- [x] **Step 2 — Failing tests for `redaction`:** a redactor masks matched secrets; `redact_text(x, None)` returns `x` unchanged.
- [x] **Step 3 — Implement** `secure_create`/`secure_open_append` in `file_io.py` by lifting the exact logic currently inside `SessionLogger.__init__`'s `LegacyFileStore` (`session_logger.py:54-81`). Implement `redaction.py` from the `_redact_text`/`_redactor` logic (`session_logger.py:127`).
- [x] **Step 4 — Refactor `SessionLogger`** to call the extracted helpers instead of owning them. Run the FULL existing `SessionLogger` test suite — behavior must be unchanged: `uv run pytest packages/provide-uterm/tests -k "session_logger or recording" -vv`.
- [x] **Step 5 — Run `run_all_tests.py`** (SessionLogger is security-critical and likely on the mutation perimeter — confirm in root `pyproject.toml [tool.mutmut]`; if `file_io.py`/`redaction.py` should be on the perimeter, add them to `paths_to_mutate`). Then `uv run python scripts/run_mutation_gate.py --changed-only`.
- [x] **Step 6 — Commit.** Two commits: `refactor(file-io): extract hardened secure_create/secure_open_append` then `refactor(session-logger): consume extracted secure-open + redaction helpers`.

**Acceptance:** A consumer writing custom logs can `from provide.uterm.file_io import secure_open_append` and `from provide.uterm.redaction import make_redactor` to inherit the 0o600/0o700/no-symlink hardening + redaction WITHOUT adopting `SessionLogger`. `SessionLogger` behavior byte-identical (existing tests green). No hardening logic duplicated.

---

### Task U4: Unify the two frame-builder families — *enabler (F1)*

**Context:** `control_channel_builders.py` returns plain `dict`s (and is the public family); `server/bridge/frames.py` returns Pydantic models. Consumers hand-build dicts because "which builder, returning what, importable from where" is unclear. Goal: one canonical, discoverable, *validating* path. **This touches frames → obey constraint #3 (codegen).**

**Files:**
- Modify: `packages/provide-uterm/src/provide/uterm/control_channel_builders.py` (make builders validate against `bridge/schemas.py` models, still returning `dict` via `.model_dump()` for wire-compat)
- Possibly create: `packages/provide-uterm/src/provide/uterm/frames.py` (a single re-export facade for ALL `make_*` builders — control + server-side)
- Modify: `packages/provide-uterm/src/provide/uterm/bridge/schemas.py` ONLY if a builder lacks a model (then run codegen)
- Test: `packages/provide-uterm/tests/test_frame_builders.py`

- [x] **Step 1 — Failing test:** every public `make_*` builder produces output that round-trips through the matching `schemas.py` model (`Model.model_validate(make_x(...))` succeeds), and invalid inputs raise (e.g. `make_session_token("")` rejects empty token — it already guards; assert it). Assert a single import site exposes the full builder set:
```python
def test_all_builders_validate_against_schema():
    from provide.uterm import frames
    from provide.uterm.bridge import schemas
    f = frames.make_resume(token="abc", player_id=1)
    schemas.ResumeFrame.model_validate(f)   # must not raise

def test_builder_facade_exposes_full_set():
    from provide.uterm import frames
    for name in ("make_resume", "make_resume_ok", "make_resume_failed",
                 "make_session_token", "make_link_patterns", "make_snapshot_frame"):
        assert hasattr(frames, name)
```

- [x] **Step 2 — Run, verify fail** (`provide.uterm.frames` facade doesn't exist; some builders don't validate).
- [x] **Step 3 — Implement.** Create `frames.py` re-exporting the control builders + (lazily, to avoid a core→server dep) document where server-side `make_snapshot_frame` lives, OR move snapshot building into the schema-backed core if it has no server-only deps. Make each core dict-builder validate against its `schemas.py` model before returning. **Do not** create a core→server import cycle — if `make_snapshot_frame` must stay server-side, the facade re-exports it only within the server package and the test for it lives there.
- [x] **Step 4 — If you added/changed any model in `schemas.py`:** `uv run python scripts/codegen_frames.py` then commit `schemas.py` + `frames.schema.json` + `frames.ts` together. Run `scripts/codegen_frames.py --check`.
- [x] **Step 5 — Run** `uv run python scripts/run_all_tests.py` (frame schemas are on the mutation perimeter — run `run_mutation_gate.py --changed-only`).
- [x] **Step 6 — Commit.** `refactor(frames): single validating builder facade across control + bridge frames`

**Acceptance:** One discoverable import (`provide.uterm.frames`) exposes all builders; each validates against its schema; no import cycle; codegen check green. Document in `CLAUDE.md`'s "Frame Schemas" section: "always build frames via `make_*`; never hand-write dicts."

---

### Task U2: Transport-agnostic guarded-send / `expect` — *blocker (F10)*

**Context:** The expect-loop semantics exist only server-side behind `HijackClient` HTTP (`client/hijack.py:231-256`). Extract a session-level primitive so the hub guard, `HijackClient`, and direct local-session consumers share one implementation. Built on existing `wait_for_screen_change`/`wait_for_update` (`transport_session.py:134-188`).

**Files:**
- Create: `packages/provide-uterm/src/provide/uterm/expect.py`
- Modify: `packages/provide-uterm/src/provide/uterm/transport_session.py` (add a thin `send_expect` method delegating to `expect.py`, so `transport_session.py` stays < 500 LOC)
- Test: `packages/provide-uterm/tests/test_expect.py`

**Interface contract:**
```python
# expect.py
@dataclass(frozen=True)
class ExpectResult:
    matched: bool
    matched_text: str | None       # the substring/regex hit, if any
    screen: str                    # final snapshot screen
    timed_out: bool

async def send_and_expect(
    session: SessionProtocol,      # anything with send + wait_for_screen_change + snapshot
    keys: str,
    *,
    expect_text: str | None = None,
    expect_regex: str | None = None,
    timeout_ms: int = 5000,
    sanitize: bool = True,         # route keys through prepare_keystrokes
) -> ExpectResult: ...
#   - if sanitize: keys = prepare_keystrokes(keys)   (client/sanitizer.py:76)
#   - send, then loop: capture screen_change_seq, wait_for_screen_change(since=seq, timeout_ms=remaining),
#     re-check expect_text/expect_regex against the snapshot until match or deadline.
```

- [x] **Step 1 — Failing tests** against a fake session (drives a scripted sequence of snapshots): (a) `expect_text` appears after 1 update → `matched=True`, `matched_text` set, `timed_out=False`; (b) never appears → `matched=False`, `timed_out=True`; (c) `expect_regex` capture works; (d) `sanitize=True` neutralizes a dangerous control sequence (assert the sent bytes were passed through `prepare_keystrokes`); (e) both `expect_text` and `expect_regex` None → returns after first settle without matching logic.
- [x] **Step 2 — Run, verify fail.**
- [x] **Step 3 — Implement** `expect.py` using `prepare_keystrokes` (import from the client `sanitizer`; if a core→client dep is undesirable, move `prepare_keystrokes` to core `sanitizer.py` and re-export from client — note this in the commit). Add `TransportSession.send_expect(...)` delegating to `send_and_expect(self, ...)`.
- [x] **Step 4 — Run** `uv run pytest packages/provide-uterm/tests/test_expect.py -vv` + the transport suite.
- [x] **Step 5 — Server bridge review.** Reviewed the server-side hijack `send` path and left it on its existing pre-send `wait_for_guard` policy/prompt guard. `send_and_expect` is a post-send local-session helper; replacing the bridge guard with it would change semantics. Run the hub/route tests.
- [x] **Step 6 — Commit.** `feat(session): transport-agnostic send_and_expect with keystroke sanitization`

**Acceptance:** A consumer holding a raw `connect_telnet(...)` session can `await session.send_expect("D\r", expect_regex=r"Command \[", timeout_ms=3000)` with built-in sanitization. The existing server hijack guard remains separate because it is a pre-send prompt/policy guard, while `send_expect` is the post-send local-session primitive. `prepare_keystrokes` is applied by default.

---

### Task U3: Flow/menu execution in the detection engine — *blocker (F8/F9); largest item*

**Context:** `RuleSet.flows`/`menus` are parsed but never executed (`rules.py:119-120`; `to_prompt_patterns` ignores them at `:123`). Add a declarative flow driver so consumers stop hand-rolling multi-stage login/char-create state machines, and surface the already-computed `kv_data`/`is_idle` out of `process_screen`.

**Files:**
- Read first: `packages/provide-uterm/src/provide/uterm/detection/rules.py` (`FlowRule:109`, `MenuRule:84`), `detection/engine.py` (`process_screen`), `detection/models.py` (`PromptDetection`/`PromptMatch`, `kv_data`), `detection/detector.py` (`negative_match:190`, two-pass `:290`).
- Create: `packages/provide-uterm/src/provide/uterm/detection/flow.py`
- Modify: `detection/engine.py` (add `advance_flow`; ensure `kv_data`/`is_idle` are reachable from the result)
- Modify: `detection/__init__.py` (export `FlowEngine`/`FlowStep`)
- Test: `packages/provide-uterm/tests/detection/test_flow.py`

**Interface contract (design test-first; this sketch is the target shape):**
```python
# flow.py
@dataclass(frozen=True)
class FlowStep:
    flow_id: str
    current_prompt_id: str | None   # prompt detected on the current screen
    next_action: str | None         # the canned input to send for this stage (from the FlowRule)
    done: bool                      # flow reached a terminal stage
    kv_data: dict[str, Any]         # extracted fields from the current screen

class FlowEngine:
    def __init__(self, ruleset: RuleSet): ...
    def advance(self, flow_id: str, screen: str, cursor: tuple[int, int] | None = None) -> FlowStep:
        # Resolve the FlowRule, run the detector restricted to that stage's gate prompts
        # (honoring negative_match so a later stage's prompt doesn't match early),
        # and return the next action + whether the flow is complete.
```
The `FlowRule` schema already exists in `rules.py:109` — read it to learn its actual fields (ordered stages, gate prompts, etc.) and build `advance` to match. If `FlowRule` is too thin to drive a flow (e.g. lacks per-stage actions), extend the model in `rules.py` (Pydantic) test-first.

- [x] **Step 1 — Read `rules.py:84-160` and `models.py`** to learn the real `FlowRule`/`MenuRule`/`PromptDetection` fields. Write down the actual field names you'll use (no guessing).
- [x] **Step 2 — Write failing tests** using a small in-test `RuleSet` with a 2-stage flow (e.g. a login: stage 1 expects "name?" → action "alice\r"; stage 2 expects "password?" with `negative_match` excluding stage 1 → action "pw\r"; then terminal). Assert: `advance("login", screen_with_name_prompt)` → `current_prompt_id` = stage-1 id, `next_action` = "alice\r", `done=False`; `advance("login", screen_with_password_prompt)` → stage-2 id, `done=False`; terminal screen → `done=True`. Assert `kv_data` is populated when the stage has `kv_extract`.
- [x] **Step 3 — Run, verify fail.**
- [x] **Step 4 — Implement `FlowEngine.advance`** reusing the existing `PromptDetector` (two-pass, `negative_match`) — do NOT write a new matcher. Reuse `KVExtractor` for `kv_data`.
- [x] **Step 5 — Surface `kv_data`/`is_idle`** from `DetectionEngine.process_screen` if not already returned (read `engine.py` first; the data is computed — just thread it to the result/`PromptDetection`). Add a regression test asserting `process_screen(...).kv_data` is non-empty for a screen with a `kv_extract` prompt.
- [x] **Step 6 — Run** `uv run pytest packages/provide-uterm/tests/detection/ -vv` then `uv run python scripts/run_all_tests.py`. Detection `detector.py` is on the mutation perimeter — `uv run python scripts/run_mutation_gate.py --changed-only`.
- [x] **Step 7 — Commit.** Two commits: `feat(detection): expose kv_data/is_idle from process_screen` then `feat(detection): add FlowEngine to drive declarative multi-stage flows`.

**Acceptance:** Given a `rules.json` with a `flows` section, a consumer can drive a multi-stage login/char-create via `FlowEngine.advance(flow_id, screen)` instead of hand-rolled substring matching; `kv_data` and `is_idle` are accessible from the engine result. Reuses the existing detector/extractor (no parallel matcher). `flows`/`menus` are no longer dead data.

---

### Task U8: Document `add_watch` as the raw-byte tap — *enabler (F4/F5)*

*Status: complete; merged into `main` and API-gap worktree removed.*

**Files:**
- Modify: docstrings in `packages/provide-uterm/src/provide/uterm/transport_session.py:192` (`add_watch`), `telnet_session.py:43` (`connect_telnet`), `ws_session.py:31` (`connect_ws`).
- Modify: `docs/EXTENSIBILITY.md` (or add a short `docs/tapping-raw-bytes.md`) — a 10-line recipe.

- [x] **Step 1 — Docstring on `add_watch`:** state it is the supported way to observe the raw, IAC-stripped byte stream before the emulator consumes it, available on ALL sessions (telnet + ws) since it lives on the base class. Include the one-liner:
```python
session.add_watch(lambda state, raw: buf.extend(raw))  # raw bytes, ANSI/CP437 intact
```
- [x] **Step 2 — Cross-link** from `connect_telnet`/`connect_ws` docstrings: "to tap raw bytes, use `session.add_watch(...)` — do not monkeypatch the emulator."
- [x] **Step 3 — Add the recipe** to `docs/EXTENSIBILITY.md` (this is a docs-only change; no SPDX needed for `.md`).
- [x] **Step 4 — Commit.** `docs(transport): document add_watch as the raw-byte tap`

**Acceptance:** A reader of `connect_telnet`/`add_watch` learns the raw-byte tap exists without reading source. (This is what would have prevented uwarp's F4 monkeypatch + F5 dead fallback.)

---

## PART B — uwarp-space adoption (different repo; gated on Part A)

> Repo: `/Users/tim/code/gh/undef-games/uwarp-space`. Edit `packages/`, never the generated `worker/src/`. uwarp has its own gates (100% coverage, hypothesis, ruff, mypy) — run `make` targets there; check `uwarp-space/CLAUDE.md` and `Makefile` before pushing. Each fix below is independent; commit one logical unit at a time.

These are listed shortest-leash first. **Tier B1** needs no uterm change (do anytime). **Tier B2/B3** are gated on the named uterm task shipping.

### Tier B1 — no uterm dependency (do now)

- **B-F2 (drop the ansi fork):** In `packages/uwarp/src/uwarp/frontend/cli/tools/ansi.py`, delete the forked `DEFAULT_PALETTE`, `_color256_to_rgb`, `_map_index`, `_convert_sgr_*`, `upgrade_to_256`, `upgrade_to_truecolor` (lines `25-216`) and replace with `from provide.uterm import upgrade_to_256, upgrade_to_truecolor` + `from provide.uterm.ansi import DEFAULT_PALETTE`. Keep `load_palette` + the click commands. **First** verify `preview_ansi` parity (uwarp's is narrower) — uterm exposes `preview_ansi = normalize_colors`. Repoint the test `uwarp-server/tests/.../test_ansi_256_upgrade.py` at the uterm symbols.
- **B-F4 (replace the monkeypatch):** In `packages/uwarp-explorer/src/uwarp/explorer/probes/_sector_fighter_helpers.py:20-57`, delete the `session._emulator.process = _tee` monkeypatch in `RawCapture`; use `session.add_watch(lambda _state, raw: buf.extend(raw))`.
- **B-F5 (delete dead fallback + fix comment):** In `packages/uwarp-explorer/src/uwarp/explorer/worker_term_bridge.py`, remove `_snapshot_poll_loop` (`:108-146`) and the stale comment (`:73-86`) claiming `TelnetSession` lacks `add_watch`. Call `add_watch` unconditionally (keep a `getattr` guard ONLY for test doubles, with a correct comment).
- **B-F6 (event-driven waits):** In `packages/uwarp-explorer/src/uwarp/explorer/io/helpers.py:219-239`, rewrite `_wait_for`/`_wait_any` to capture `screen_change_seq()` then `await wait_for_screen_change(timeout_ms=..., since=seq)` between checks (keep the existing `getattr` fallback for stubs), matching the sibling `_send` helper.
- **B-F7 (fix misleading comments):** In `case_library_runner_reconnect.py:36-41,118-119` and `tests/compare/test_establish_baseline_force_reconnect.py:8`, reword "resilient/auto-reconnecting" to "uterm's session has no reconnect/keepalive — reconnection is handled here." **Do not remove the reconnect code.**
- **B-F10-sanitizer (half of F10):** In `packages/uwarp-explorer/src/uwarp/explorer/mcp/server.py`, route the guarded-send keystrokes through `from provide.uterm.client.sanitizer import prepare_keystrokes`.
- **B-F1-handshake (handshake frames — builders already exist & exported):** Replace raw dict literals with `make_resume`/`make_resume_ok`/`make_resume_failed`/`make_session_token`/`make_link_patterns` (from `provide.uterm` / `control_channel_builders`) at `_ts_bridge.py:115`, `_ws_protocol.py:113,119,160,168`, `_control_frames.py:141`. Use `make_snapshot_frame` from `provide.uterm.server.bridge.frames` at `worker_term_bridge.py:241` (the explorer is a server-side consumer, so it may import the server pkg). *(U4 makes this nicer but is not required — the builders already exist.)*

### Tier B2 — gated on Part A

- **B-F3 (gated on U7):** Replace the hand-rolled DLE-STX parser at `_ws_protocol.py:62-84` with `provide.uterm.control_channel.is_control_framed(message)`.
- **B-F12 (gated on U6):** Change `packages/uwarp-server/src/uwarp/runtime/sysop/watch.py:12` from `from provide.uterm.deckmux._hub_mixin import DeckMuxMixin` to `from provide.uterm.deckmux import DeckMuxMixin`; update the `_deckmux_init()` call to the public `deckmux_init()`.
- **B-F11 (gated on U5):** In `compare_log/_streams.py:162-173`, if logs may contain session secrets, route raw screen capture through the extracted `provide.uterm.file_io.secure_open_append` + `provide.uterm.redaction.make_redactor` (keep the multi-target diff/formatting layer custom). Also delete the stale `.uterm-recordings/scratch.jsonl`.
- **B-F10-session (gated on U2):** Refactor the explorer MCP `session_send/snapshot/step/release` tools (`mcp/server.py:195-254`) onto `session.send_expect(...)` where they drive a local session; keep the game-domain tools custom. **Status:** landed in `uwarp-space/main`; local send/snapshot/step use `send_expect`, release remains control-plane custom by design and is covered by tests.

### Tier B3 — gated on Part A, larger

- **B-F8 (gated on U3):** Consume `det.kv_data` for sector/credits/port instead of the parallel regexes (`worker_runtime_execution.py:23`); match menus via `det.match.prompt_id` against existing rule IDs (`:41-43`) instead of new module-level regexes. **Status:** landed in `uwarp-space/main`; sector refresh now consumes detector `kv_data`/prompt IDs, and `prompt.sector_command` extracts bracket-style `Command [TL=...]:[877]` sectors so no local sector regex fallback remains.
- **B-F9 (gated on U3):** Drive login/character-creation through `FlowEngine.advance(...)` + the existing `_dispatch_known_prompt` map (`worker_runtime_transport.py:150-184`) instead of `login.py:113-240`'s substring state machine. The matching prompts already exist in `games/tw2002/rules.json`; add a `flows` section there. **Status:** partially landed in `uwarp-space/main`; `flow.worker_runtime_prompts` drives runtime prompt recovery and `flow.twgs_character_login` drives TWGS character-login prompts, with former regex fallback prompt variants moved into rules. Live TWGS smoke as `merchant` reached the command prompt after the character-login migration. Remaining active work: migrate `login_twgs.py` pre-character BBS/game-selection routing and direct uwarp `login.py` routing to FlowEngine.

### Not doing (documented decisions)

- **F13** (bespoke admin JWT): leave as-is unless uwarp needs federated/IdP login. uterm's auth modes don't map onto the game-login model.
- The legitimate workarounds in §4 (Pyodide grammar mirror, CR/LF-preserving parsers, CF transport copy, reconnect/relogin orchestration): **leave alone** — each is documented and justified.

---

## 8. Cross-reference matrix

| uwarp finding | Category | uterm change required | uwarp fix (Part B) | Blocker? |
|---|---|---|---|---|
| F1 | Reimplementation | U4 (polish; builders already exist) | B-F1-handshake | enabler |
| F2 | Reimplementation | — (uterm already exports it) | B-F2 | n/a |
| F3 | Reimplementation | U7 | B-F3 | enabler |
| F4 | Reimplementation | U8 (docs) | B-F4 | enabler |
| F5 | Workaround (bad) | U8 (docs) | B-F5 | enabler |
| F6 | Workaround (bad) | — (API exists) | B-F6 | n/a |
| F7 | Misleading docs | U1 (reduces need) | B-F7 | enabler |
| F8 | Under-adoption | U3 | B-F8 | **blocker** |
| F9 | Under-adoption | U3 | B-F9 | **blocker** |
| F10 | Under-adoption | U2 (session half); sanitizer needs nothing | B-F10-* | **blocker** (session half) |
| F11 | Under-adoption/security | U5 | B-F11 | **blocker** |
| F12 | Private-API coupling | U6 | B-F12 | **blocker** |
| F13 | Under-adoption | — | (not doing) | n/a |

---

## 9. Sequencing & priority

**Part A order (easiest → hardest; respects dependencies):**
1. **U6** (export DeckMuxMixin) — mechanical, minutes.
2. **U7** (`is_control_framed`) — mechanical.
3. **U1a** (keepalive forward) — one-liner + defaults.
4. **U8** (docs) — no code risk.
5. **U5** (secure-open/redaction extraction) — refactor, security-perimeter, existing tests guard behavior.
6. **U4** (frame-builder facade) — touches codegen; do after U-mechanicals.
7. **U1b** (reconnect wrapper) — new module.
8. **U2** (send_and_expect) — new module + server refactor.
9. **U3** (FlowEngine) — largest; do last.

**Part B order:** all of Tier B1 can proceed immediately (independent of Part A). Tier B2/B3 each unlock when their gating uterm task ships and uwarp's editable install picks it up (immediate, since it's a path install).

**Suggested commit/PR grouping (Part A):** U6+U7+U1a+U8 in one PR (low-risk batch), U5 alone, U4 alone, U1b+U2 together (transport ergonomics), U3 alone.

---

## 10. Self-review (run by plan author)

**Spec coverage:** Every finding F1–F13 maps to a task or a documented "not doing" (§8 matrix). Every uterm gap U1–U8 has files + tests + acceptance. ✅

**Placeholder scan:** No "TBD/TODO/handle edge cases" left. The two largest items (U2, U3) give real interface contracts + concrete test cases and explicitly instruct read-then-TDD because their final code depends on existing internal field names the executor must read first (`FlowRule` fields, the hijack send path) — this is flagged, not hand-waved. ✅

**Type/name consistency:** `send_and_expect`/`send_expect`, `ExpectResult`, `ReconnectPolicy`/`connect_with_reconnect`/`ReconnectingSession`/`on_reconnect`, `FlowEngine.advance`/`FlowStep`, `secure_create`/`secure_open_append`/`make_redactor`/`redact_text`, `is_control_framed`, `DeckMuxMixin`/`deckmux_init` are used consistently across §5, §6, §8. ✅

**Constraint coverage:** codegen (U4), 500-LOC splits (U2/U3/U5 put new logic in new files), mutation perimeter (U3/U4/U5 call it out), SPDX on new `.py` (templated in U6 test), defaults-not-inline-numbers (U1). ✅

---

## Appendix — provenance

This plan was produced from a five-track parallel audit of uwarp-space's uterm usage (session/transport, detection, ANSI/screen/emulator, control-channel/bridge/deckmux, server/transports/gateway/MCP) on 2026-06-09, with the §3 facts re-verified directly against source. uwarp consumes uterm cleanly (editable installs, no fork) and uses it well at the architectural seams; this plan targets the higher-level features uwarp re-derives because uterm either doesn't expose them ergonomically (U1/U2/U3/U5), keeps them private (U6), or doesn't advertise them (U7/U8) — plus one DRY consolidation (U4).
