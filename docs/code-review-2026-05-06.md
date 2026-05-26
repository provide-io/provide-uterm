# Comprehensive Code Review & Architectural Analysis: provide-uterm

_Reviewed 2026-05-06 against commit `b5c6a73` on `main`._

> Historical snapshot: this report references pre-refactor architecture terms
> and file paths that have since changed. Use it for historical context, not as
> current architecture/source-of-truth documentation.

## Context

`provide-uterm` is a terminal session platform: it brokers PTY/SSH/telnet/local-shell sessions across browsers and remote workers, with hijack-leasing for viewer/operator/admin roles, recording, MCP/AI integration, and a Cloudflare Worker + Durable Object backend that mirrors the FastAPI hub. Monorepo with 7 packages (uv workspace + npm workspaces). Quality bar set at 100% branch coverage + 100% mutation kill rate, ruff/mypy strict/ty/bandit/xenon/vulture in pre-commit.

This review reads the codebase as-is and produces a calibrated assessment: what is well-built, what carries risk, and what concrete actions would move the needle. Findings have been verified at file:line — claims that didn't survive verification have been removed or downgraded.

**Overall grade: A−.** The bridge architecture is genuinely well-factored, security defaults are deliberate (not accidental), test discipline is exemplary, and there is zero TODO/FIXME debt. The chunkier issues are fat module files, CF Worker import gymnastics, and a few operational gaps (recording-at-rest hygiene, fanout authorization clarity).

---

## What is well-built

### 1. The 3-layer bridge architecture

The hub/bridge/mixin split is the spine of the codebase and it holds up under scrutiny.

- **`HijackableMixin`** at `packages/provide-uterm-server/src/provide/terminal/bridge/base.py:25` — gives any worker pause/resume/step/watchdog primitives via cooperative checkpoints (`await_if_hijacked()`, `request_step(checkpoints=2)`). On WS disconnect the mixin force-clears `_hijacked` (`worker_link.py:342`) so a dropped link can't leave a worker stuck paused. That's the right invariant.
- **`TermHub`** at `packages/provide-uterm-server/src/provide/terminal/bridge/hub/core.py:87` is a 168-line orchestrator that composes 5 focused mixins (approval flow, messaging, state, polling, hijack ownership, connections). Each mixin is small and topical. State guarded by a single `asyncio.Lock`.
- **`TermBridge`** at `packages/provide-uterm-server/src/provide/terminal/bridge/worker_link.py:106` — worker WS client with exponential reconnect, permanent-failure detection (401/403/404/InvalidURI), and clean send/recv loops.
- **`HijackCoordinator`** at `packages/provide-uterm-server/src/provide/terminal/bridge/coordinator.py:43` is the single source of lease arbitration logic, reused by both FastAPI hub and the CF Durable Object — no duplicated state machine.

### 2. Control-channel codec

`packages/provide-uterm/src/provide/terminal/control_channel.py:72` — DLE/STX framing with an 8-hex-digit length prefix lets raw terminal bytes and JSON control frames coexist on a single WebSocket. Streaming decoder yields `DataChunk | ControlChunk`. Frontend codec at `packages/provide-uterm-frontend/.../hijack-codec.ts` mirrors it precisely. `FrameType` is a closed union of 18 variants in `packages/provide-uterm/src/provide/terminal/bridge/contracts.py:170`. Tight, single source of truth.

### 3. Auth validation — well-defended, not accidentally open

The headline "dev mode is the default" (line 322 of `config_schema.py`) looks alarming until you read `_validate_auth_config()` at `packages/provide-uterm-server/src/provide/terminal/server/app.py:101-146`:

- Dev/none mode raises `RuntimeError` if `server.host` is not loopback (`127.0.0.1`/`localhost`/`::1`)
- `auth.require_jwt_in_production=true` hard-blocks dev/none at startup
- `auth.mode='header'` requires explicit `header_mode_acknowledged=true` ack
- `'none'` algorithm is forbidden in `jwt_algorithms`
- JWT mode requires either `jwt_public_key_pem` or `jwt_jwks_url`
- Loud `logger.warning` in dev mode

There is also a guardrail test suite at `packages/provide-uterm-server/tests/server/test_auth_guardrails.py`. This is good defense-in-depth.

### 4. Test & quality posture

| Package | src files | test files | ratio |
|---|---|---|---|
| provide-uterm | 101 | 186 | 1.84× |
| provide-uterm-server | 100 | 261 | 2.61× |
| provide-uterm-platform | 30 | 51 | 1.70× |
| provide-uterm-cloudflare | 37 | 63 | 1.70× |
| provide-uterm-client | 17 | 43 | 2.52× |

- **0** TODO/FIXME/XXX/HACK markers across the source tree.
- **0** bare `except:`.
- 100% branch + line coverage gate + 100% mutation kill rate (per `CLAUDE.md`).
- 7 ARDs in `docs/` map cleanly to implemented modules (spot-checked against `bridge/fanout/` and `recording.py`).
- Explicit `__all__` in 42 of 45 reviewed `__init__.py` files; the 3 exceptions are intentional (lazy load / empty stubs).

### 5. Connector security defaults fail-closed

`packages/provide-uterm-server/src/provide/terminal/server/connectors/ssh.py:85-91` — if `known_hosts` is missing, the connector **raises `ValueError`** unless the operator explicitly opts in via `insecure_no_host_check=true` (which logs a warning). That is the correct default polarity.

### 6. Logging hygiene

- JWT failure logs the exception, not the token (`server/auth.py:145`).
- Pubkey rejection logs fingerprint, not the key.
- Tunnel-token validation logs `session_id` only.

---

## Real concerns (ranked)

### P1 — Fat top-of-stack modules

Three files dominate the LOC distribution and should be carved up:

| File | LOC | Issue |
|---|---|---|
| `packages/provide-uterm-server/src/provide/terminal/server/app.py` | 805 | Mixes FastAPI factory, auth validation, connector registration, middleware stacking, route wiring. |
| `packages/provide-uterm-cloudflare/src/provide/terminal/cloudflare/entry.py` | 768 | CF Worker entrypoint plus 56 suppression annotations (44 `type: ignore` + 12 `pragma: no cover`) for a triple-import Pyodide fallback strategy. |
| `packages/provide-uterm-cloudflare/src/provide/terminal/cloudflare/do/session_runtime.py` | 613 | Already started extracting `_SessionRuntimeIoMixin` + `_WsHelperMixin`; finish the job by extracting hijack coordination. |

**Recommendation:** split `app.py` into `app.py` (factory only), `app_auth.py` (`_validate_auth_config` + identity provider wiring), and `app_connectors.py` (`_register_builtin_connectors`). Move CF Pyodide stub assignments out of `entry.py` into `cf_fallback_stubs.py` with a single blanket suppression block.

### P2 — Recording is plaintext on disk with no secret filtering

`packages/provide-uterm/src/provide/terminal/recording.py` — `LocalFileRecordingStore` writes JSONL to the filesystem. `config_schema.py:344` defaults `session_retention_s=0` (indefinite). PTY input that includes typed passwords is recorded verbatim; `control_channel_mode` defaults to `"exclude"` which keeps control frames out, but **does nothing about secrets typed at the prompt**.

**Recommendation:** add a configurable input redactor (regex denylist applied to `input` frames before persistence) and document the "passwords typed at TTY are recorded" caveat in the operator-facing docs. Consider a default retention floor (e.g., 30 days) when recording is enabled.

### P3 — CF Worker → Durable Object trust boundary is implicit

`packages/provide-uterm-cloudflare/src/provide/terminal/cloudflare/do/session_runtime.py` — DO methods assume the calling Worker has already authenticated. There is no service-to-service token between them. Within a single CF account this is the standard model, but if a sibling Worker is ever added to the same DO namespace it inherits full session access.

**Recommendation:** add a short comment block at the DO entry point documenting the trust assumption ("only the same-account Worker is permitted to invoke this DO; no in-DO authn is performed"), and confirm CF Access service-token verification (`entry.py:_has_cf_service_token`) is the only "trust the caller" hop.

### P4 — MCP/AI tool authorization is per-tool, not centralized

`packages/provide-uterm-client/src/provide/terminal/ai/server.py` exposes 21 tools. `session_create` (can spawn arbitrary connector configs), `worker_input_mode` / `session_set_mode` (input-mode flips), and `fanout_send` (broadcast) are powerful. Authorization currently lives inside each tool implementation rather than at a single MCP-layer chokepoint.

**Recommendation:** wrap MCP tool dispatch in a single decorator that resolves the principal and checks role before delegating to the tool body. Audit `session_create` specifically — confirm connector configs from MCP callers are vetted against an allowlist or principal scope.

### P5 — Silent exception swallows in CF entry.py

Read-and-verify confirmed three of the four flagged sites are appropriate (URL-parse fallbacks, header presence checks). One worth a follow-up:

- `packages/provide-uterm-cloudflare/src/provide/terminal/cloudflare/entry.py:266-267` — `except Exception: pass` on header inspection. Adding a `logger.debug` inside the except keeps the same control flow without losing ops visibility when something genuinely surprising happens.

### P6 — `type: ignore` density in CF package

546 suppression annotations total; 291 (53%) live in `provide-uterm-cloudflare`. This is intrinsic to the Pyodide flattening strategy (vendored `python_modules` tree, triple-import fallback) but it does erode mypy/ty signal in that package. The mitigation in P1 (extract stubs to a separate file with one blanket suppression) addresses most of it.

---

## Patterns to keep

- **Mixin composition for `TermHub`** — every mixin is under 300 LOC and topical; resist the urge to flatten.
- **Single `HijackCoordinator`** shared by FastAPI and CF — never let lease state-machine logic fork.
- **TypedDict + `FrameType` union** — keeps frontend/backend codec drift detectable. Add a CI gate that fails if `FrameType` adds a member without a matching change in `hijack-codec.ts` if not already present.
- **`require_jwt_in_production` guardrail flag** — keep teaching operators to set it.

---

## Patterns worth re-examining

- **Lazy `__getattr__` in `provide/terminal/__init__.py`** — pleasant for import-time perf, but defeats static analyzers and IDE jump-to-def. Consider whether the savings still justify the cost; a reorganized public API with explicit imports may be cheaper now than it was at v0.1.
- **Dual lease model** (REST `HijackSession` + dashboard WS `hijack_owner`) — both can hold a lease simultaneously and the conflict resolution (first-write-wins inside `_dispatch_control_msg`) is implicit. Documenting the contract — or asserting it via a test that exercises both holders concurrently — would prevent future regressions.
- **`WorkerTermState.events` deque(maxlen=2000)** — silent drop. If audit completeness matters, raise a metric counter when the deque is at cap.

---

## What this review did NOT cover (deliberate scope cuts)

- Frontend / TypeScript code beyond the codec mirror (`hijack-codec.ts` was spot-checked).
- Performance / load characteristics — this is a static review, not a benchmark.
- Network & deployment topology (Docker / K8s manifests).
- Specific fuzzing or property-based testing of `ControlChannelDecoder`.
- Threat modeling for the share-token / tunnel flow (only the auth-gate path was traced).

Any of the above are reasonable follow-ups if the team wants a deeper pass.

---

## Critical files referenced

- `packages/provide-uterm/src/provide/terminal/bridge/contracts.py:170` — FrameType union (single truth).
- `packages/provide-uterm/src/provide/terminal/control_channel.py:72` — DLE/STX codec.
- `packages/provide-uterm-server/src/provide/terminal/bridge/base.py:25` — HijackableMixin.
- `packages/provide-uterm-server/src/provide/terminal/bridge/hub/core.py:87` — TermHub.
- `packages/provide-uterm-server/src/provide/terminal/bridge/hub/ownership.py` — `_expire_leases_under_lock`.
- `packages/provide-uterm-server/src/provide/terminal/bridge/coordinator.py:43` — HijackCoordinator.
- `packages/provide-uterm-server/src/provide/terminal/bridge/worker_link.py:106-342` — TermBridge.
- `packages/provide-uterm-server/src/provide/terminal/bridge/routes/websockets.py:112,225` — worker WS handler (cleanup_task verified cancelled).
- `packages/provide-uterm-server/src/provide/terminal/server/app.py:101-146` — `_validate_auth_config` (verified well-defended).
- `packages/provide-uterm-server/src/provide/terminal/server/auth.py:131,203-210` — JWT validation.
- `packages/provide-uterm-server/src/provide/terminal/server/connectors/ssh.py:85-97` — fail-closed host-key default.
- `packages/provide-uterm/src/provide/terminal/recording.py` — plaintext recording store.
- `packages/provide-uterm-cloudflare/src/provide/terminal/cloudflare/entry.py` (768 LOC, 56 suppressions).
- `packages/provide-uterm-cloudflare/src/provide/terminal/cloudflare/do/session_runtime.py` (613 LOC).
- `packages/provide-uterm-client/src/provide/terminal/ai/server.py` — 21 MCP tools.

---

## Suggested next actions (if you want to act on this)

1. **P1 split**: refactor `app.py` and `entry.py` per recommendations above. Estimated 1 working day; no behavior change, pure structure.
2. **P2 input redactor**: add `RecordingConfig.input_redact_patterns: list[str]` and apply it in `recording.py` before persisting `input` frames. Half a day plus tests.
3. **P3/P4 docs + chokepoint**: add MCP-tool authorization decorator; add CF DO trust-boundary docstring. Half a day.
4. **P5/P6 cleanup**: log-then-pass on the one CF except site; extract Pyodide stubs to a dedicated module. Half a day.

These are independent and can be sequenced or parallelized as desired.

## Verification (how to validate any follow-up changes)

- `uv run python scripts/run_pytest_gate.py -q` — full quality gate (ruff/mypy/pytest/xenon/vulture).
- `uv run pytest packages/provide-uterm-server/tests/server/test_auth_guardrails.py -v` — verify auth guardrails still trigger after `app.py` split.
- `uv run python scripts/run_all_tests.py` — every package's pytest with its own coverage config.
- For UI-touching changes: `npm run typecheck:frontend && npm run lint:frontend` and `uv run pytest -m playwright`.
- For mutation regressions on changed modules: `uv run python scripts/run_mutation_gate.py --changed-only --min-mutation-score 100`.
