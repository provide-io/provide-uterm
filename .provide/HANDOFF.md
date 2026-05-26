# Handoff: Test-Suite Resilience Sweep

## Context (read first)

`main` shipped a flake fix in commit `8455a33` after CI scheduled-run
`26397225022` failed on Python 3.14 with one test:

> `tests/e2e/multi_browser/test_concurrent.py::test_three_role_browsers_all_receive_snapshot_eventbus_delivers — assert 0 == 1`

Root cause: `await asyncio.sleep(0.1)` used to "wait" for an EventBus
long-poll subscriber to register before the worker fired its event.
Python 3.14's asyncio scheduler changed task-wakeup timing enough that
100ms wasn't always enough — the snapshot was appended before any
subscriber existed, so the poll returned 0 events.

The repo already had a deterministic helper
(`tests/e2e/_live_server.py::wait_for_subscribers`) whose docstring
literally calls this pattern out. The two affected tests just hadn't
been migrated. Fix: replace the sleep with
`wait_for_subscribers(hub, "s1", 1)`.

Several adjacent fragile patterns remain. **Don't** generalize the fix
to every `asyncio.sleep(...)` site — most are legitimate. Only the
patterns below.

## Reference points

- The flake fix: commit `8455a33`
- Canonical subscription waiter: `tests/e2e/_live_server.py::wait_for_subscribers`
- Server suite is at 100% coverage; the gate is in
  `packages/provide-uterm-server/pyproject.toml`
- Run server tests: `uv run pytest packages/provide-uterm-server/tests/`
- Run full workspace tests: `uv run python scripts/run_all_tests.py`
- Memray tests are deselected by default: `uv run pytest -m memray`

## Tasks

### 1. Replace "wait-for-nothing" negative assertions (medium priority, low risk)

**Symptom**: tests prove a filter rejects events by sleeping and
asserting `not delivered`. On a slow runner the event might have been
delivered late and the assertion is vacuously true — silent under-test.

**Sites** (find with `grep -rn "asyncio.sleep" packages/provide-uterm-server/tests/e2e/observer/`):

- `tests/e2e/observer/test_sse.py:157` — telnet event must not reach shell SSE
- `tests/e2e/observer/test_sse.py:227, 264` — pattern / event-type filter rejection
- `tests/e2e/observer/test_webhooks_e2e.py:172` — `event_types` filter

**Pattern to apply** for each:

```python
# Was:
await worker.send(filtered_event)
await asyncio.sleep(0.15)
assert not collect_task.done()

# Becomes:
await worker.send(filtered_event)
await worker.send(should_be_delivered_event)  # positive control
delivered = await asyncio.wait_for(collect_task, timeout=3.0)
assert len(delivered) == 1
assert delivered[0]["...marker..."] == should_be_delivered_event["..."]
```

Each test needs a "positive control" event that's known to pass the
filter under test, so the assertion becomes "exactly one event
delivered, and it's the right one."

### 2. Consolidate duplicate subscription-wait helpers (low priority, low risk)

Two near-identical helpers exist:

- `tests/e2e/_live_server.py::wait_for_subscribers` — canonical
- `tests/e2e/multi_browser/conftest.py::wait_for_event_subscriber` — duplicate

Delete the multi_browser variant, update its callers to import from
`_live_server`, verify tests still pass.

### 3. SSH / telnet transport-level waiters (lower priority — these don't currently flake)

**Sites** (find with `grep -rn "asyncio.sleep(0\.[1-9]" packages/provide-uterm-server/tests/e2e/test_ssh_*.py packages/provide-uterm-server/tests/e2e/test_telnet_*.py`):

~10 sleeps across:

- `tests/e2e/test_ssh_concurrent_identities.py`
- `tests/e2e/test_ssh_authorized_keys_rotation.py`
- `tests/e2e/test_ssh_full_chain.py`
- `tests/e2e/test_telnet_gateway.py`

Each "send → sleep → assert" pair is waiting for an asyncssh/telnet
stream to surface bytes the test asserts on. Add a
`wait_for_output(stream, marker_re, timeout=5.0)` helper to
`tests/e2e/_live_server.py` that reads from the stream until
`marker_re` matches the accumulated buffer, then migrate sites in
order of historical flakiness.

**Don't** bulk-migrate blind — some sleeps are intentional pre-send
settles. Use:

```
git log -S "asyncio.sleep(0.2)" --since=6.months -- \
  packages/provide-uterm-server/tests/e2e/test_ssh_*.py
```

to find which sleeps were added in response to flakes.

### 4. Expand memray coverage (medium priority, no flake exposure)

**Current** memray tests cover `ANSI SGR`, `ControlChannel encode/decode`,
`TermHub 200×50 workers`. Layout:

- `packages/provide-uterm/tests/memray/test_*.py` runs subprocess
  `python -m memray run` + `memray stats`, parses
  `Total allocations:` regex, asserts within ±15% of
  `tests/memray/baselines.json`.
- Stress scripts: `packages/provide-uterm/scripts/memray_*_stress.py`.

**Add** stress tests for these allocation-heavy hot paths:

| Subsystem | Hot path | Stress workload |
|---|---|---|
| EventBus fan-out (`bridge/hub/event_bus.py`) | `append_event` → deep-copies into N subscriber queues per event | 100 subscribers × 10k events |
| Webhook dispatcher (`server/webhooks.py`) | JSON serialization per dispatch | 10 webhooks × 5k events |
| Tunnel intercept (`tunnel/intercept.py`) | Request/response copies | 5k intercepted requests |
| DeckMux presence (`deckmux/_hub_mixin.py`) | `generate_name` / `generate_color` / `generate_initials` per connection | 1k connections, full presence cycle |

For each, the pattern is identical to existing memray tests:

1. Write `packages/provide-uterm/scripts/memray_<name>_stress.py` that
   does the workload synchronously (or via `asyncio.run(main())`).
2. Copy `tests/memray/test_hub_stress.py` and swap the script name +
   baseline key.
3. Run once with
   `MEMRAY_UPDATE_BASELINE=1 uv run pytest -m memray tests/memray/test_<name>_stress.py`
   to record the initial baseline.
4. Commit `baselines.json` + the new test + stress script.

These tests aren't in the default suite (they have `@pytest.mark.memray`
and `@pytest.mark.slow`); run them with `uv run pytest -m memray`.

## Verification

- **Local**: `uv run python scripts/run_all_tests.py` — server suite
  must stay at 100% coverage, 3611+ passing.
- **CI**: green on push + green on scheduled run on all four Python
  versions (3.11, 3.12, 3.13, 3.14).
- **Memray**: `uv run pytest -m memray packages/provide-uterm/tests/memray -v`
  passes (and new baselines committed if added).

## Anti-patterns to avoid

- **Do not** bulk-sed `asyncio.sleep(...)` away — most are legitimate
  (polling loops, transport settles).
- **Do not** raise sleep values to "fix" timing flakes — that's the
  failure mode this plan is trying to eliminate.
- **Do not** add `# pragma: no cover` to broaden the coverage gate
  workaround unless the path is genuinely unreachable from in-process
  tests.
- **Do not** add new tests for `bridge/hub/approvals.py:create_approvals_router`
  — that function was deleted (commit `f095105`); the canonical router
  is `server/routes/approvals.py`.

## Resolution

All four tasks closed. Captured here as historical record; the document
is finished work.

### Task 1 — sleep+negative-assert sites

Five sites migrated to the positive-control pattern in commit
`3000740` (test_sse.py:157,227,264; test_webhooks_e2e.py:172;
test_filters.py:153). Three remaining "nothing happens" sites
(test_webhooks_e2e.py:209,299,308) had no positive control available;
two of those were tightened with `wait_for_condition` fast-fail probes
in commit `f8374db`, the third left as a documented upper-bound sleep.

### Task 2 — duplicate subscription-wait helper

`multi_browser/conftest.py::wait_for_event_subscriber` deleted and
test_roles.py rerouted to `_live_server.wait_for_subscribers` in
commit `4ee826b`.

### Task 3 — SSH/telnet transport-level waiters

`wait_for_condition` helper added in commit `4a064a2`; two SSH e2e
sites migrated as POC in same commit. Remaining six SSH sites
(test_ssh_concurrent_identities.py:151,219;
test_ssh_authorized_keys_rotation.py:148,191,246,259) migrated in
commit `1022d9c`. Telnet sites were classified as fundamentally
negative-only and left intact.

### Task 4 — memray coverage expansion

Four stress tests + baselines added in commit `9b7c676`:
- EventBus fan-out: 71,997 allocations baseline
- Webhook dispatcher: 1,495,338 allocations baseline
- Tunnel intercept: 13,461 allocations baseline
- DeckMux presence: 22,287 allocations baseline

All marked `@pytest.mark.memray`, kept out of the default suite.

### Related companion work in the same session

- Server coverage gate raised from 97 → 100 (`8069514`).
- Client + platform lazy-pragma audit (`234cae5`, `bb81d0b`).
- Frontend Playwright e2e for the security fixes (`5be3973`).
- Architecture refactors #15/#16/#17/#18 + DeckMux service extraction.
