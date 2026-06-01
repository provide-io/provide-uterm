# Design: Decompose `HostedSessionRuntime` and `SessionRegistry`

## Problem

`packages/provide-uterm-server/src/provide/uterm/server/runtime.py` (488 LOC, class `HostedSessionRuntime` at `runtime.py:89`) and `packages/provide-uterm-server/src/provide/uterm/server/registry.py` (473 LOC, class `SessionRegistry`) carry the full session lifecycle as two large classes. The hottest method, `HostedSessionRuntime._run()` at `runtime.py:427`, mixes connector startup, WebSocket connect-with-backoff, permanent-vs-transient error classification, and shutdown bookkeeping in a single loop. `_bridge_session()` at `runtime.py:366` interleaves outbound queue draining, inbound recv polling, connector polling, and backoff for empty polls. *(Decomposition has not been done; the line numbers here are as of 2026-06 and drift as the files change — treat them as approximate.)*

## Concerns currently mixed in `HostedSessionRuntime`

- Connector ownership (`_start_connector` `runtime.py:240`, `_stop_connector` `runtime.py:261`).
- WebSocket transport + auth header + backoff (`_run` `runtime.py:427`).
- Bidirectional message pumping (`_bridge_session` `runtime.py:366`).
- Recording / event logging (`_log_*` family at `runtime.py:235-272`).
- Public lifecycle facade (`start`/`stop`/`restart`/`set_mode`/`clear`/`analyze` at `runtime.py:124-179`).

`SessionRegistry` mixes:

- Definition CRUD + validation (`_validate_create_payload` `registry.py:174-197`, `create_session` `registry.py:199-225`, `update_session` `registry.py:227-247`).
- Runtime instantiation (`_runtime_for` `registry.py:125-138`).
- Ephemeral-session grace-period GC (`_on_worker_empty` `registry.py:87-112`).
- Recording I/O proxying (`_flush_runtime_recording` `registry.py:411`, `recording_*` at `registry.py:417-end`).
- Event streaming (`watch_session_events` `registry.py:334`, `stream_session_events` `registry.py:376`).

## Options

| Option | Pro | Con |
|---|---|---|
| **A. Extract collaborators behind a facade.** Pull out `ConnectorHost`, `BridgeTransport`, `RuntimeRecorder`. `HostedSessionRuntime` keeps the same public surface, delegates to collaborators. | Public API unchanged → no caller churn. Each collaborator independently testable. Mutation-test pain (currently 100% kill rate on long methods) drops. | Largest diff. Requires deciding ownership of `_queue`, `_queue_bytes`, `_state` — these straddle transport vs facade. |
| **B. Composition rewrite.** Replace `HostedSessionRuntime` with a small coordinator that holds 3-4 collaborator objects, each with its own lifecycle. | Cleanest end state. Each collaborator's state is local. | Big-bang refactor; high risk to the 100% coverage gate. Likely needs to land behind a feature flag or in a long-lived branch. |
| **C. Targeted method extraction.** Leave class boundaries alone; split `_run` into `_run_one_attempt` + `_classify_error`, split `_bridge_session` into `_drain_outbound` + `_pump_inbound`. | Smallest diff, lowest test risk, immediate readability win. | Doesn't fix the underlying "god class" problem; future feature additions still pile onto the same class. |

For `SessionRegistry`, the analogous extractions would be:

- `SessionDefinitionStore` (the `_sessions` dict + validation + CRUD).
- `RuntimeSupervisor` (the `_runtimes` dict + auto-start + shutdown).
- `EphemeralLifecycleManager` (the `_on_worker_empty` grace period at `registry.py:87`).
- `RecordingFacade` (recording-related methods at `registry.py:411-end`).

## Recommendation

**Maintainer judgment call.** Both files are at the threshold where decomposition pays off but neither is failing today. My weak preference: **C first, then A as a follow-up.** Specifically:

1. Land **C** as a single PR: extract `_run_one_attempt(ws_url, headers)`, `_classify_run_error(exc) -> RunOutcome`, `_drain_outbound_once(ws)`, `_pump_inbound_once(ws, ...)`. Diff stays small, mutation coverage is easier to maintain, and the resulting method bodies become a near-mechanical map onto **A**'s collaborators.
2. Once **C** is stable, do **A** as a structural refactor — at that point the collaborator boundaries are already drawn by C's extracted methods.

Avoid **B** unless there's a feature (e.g. a second non-WebSocket transport) that actually requires the cleaner end state.

## Files that would change

For option C (the recommended first step):

- `packages/provide-uterm-server/src/provide/uterm/server/runtime.py:325-442` — split `_bridge_session` and `_run` as described.
- `packages/provide-uterm-server/src/provide/uterm/server/runtime.py:220` (`_enqueue_messages`) — move buffer accounting (`_queue_bytes`) into a small `OutboundBuffer` helper near top of file.
- `packages/provide-uterm-server/tests/test_runtime*.py` — add cases per extracted method; current tests mostly assert via end-to-end paths and will benefit from finer hooks.

For option A (follow-up):

- New module `packages/provide-uterm-server/src/provide/uterm/server/runtime/` package:
  - `connector_host.py` (extracted from `runtime.py:204-234`).
  - `bridge_transport.py` (extracted from `runtime.py:325-442`).
  - `recorder.py` (extracted from `runtime.py:235-272`).
- `packages/provide-uterm-server/src/provide/uterm/server/runtime.py` becomes the facade (~120 LOC).
- For the registry split: new modules `session_store.py`, `runtime_supervisor.py`, `ephemeral_lifecycle.py` — refactor `registry.py:47-end`.

## Open question for maintainer

The `_recording_*` methods on the registry (`registry.py:411-end`) are arguably their own service today, accessed via the registry only for convenience. Worth pulling those out independently of either option above?
