# Lane A5 — Core Library Correctness Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Read `00-ORCHESTRATION.md` "Global constraints" first. Frame schemas + several core modules are on the **mutation perimeter** — run the mutation gate before done.

**Goal:** Fix the transposed-resize bug, make detector pattern updates atomic, align the two control-plane backends, and close the coverage-perimeter gap on `auth.py`/`control/`.

**Scope (exclusive write ownership):** `packages/provide-uterm/**` only — including `packages/provide-uterm/pyproject.toml` (this package's own coverage config, incl. its `partial_branches` copy).

**Tech Stack:** Python, pyte, Pydantic v2, aiosqlite, pytest.

**Order:** CB-1 → CORE-det → CORE-iso → CORE-ord → CORE-id → CORE-cov.

---

### Task 1 (CB-1 🔴 High): Fix transposed terminal resize

**Files:**
- Modify: `packages/provide-uterm/src/provide/uterm/emulator.py:178`
- Test: `packages/provide-uterm/tests/` emulator test module

**Problem:** `self._screen.resize(cols, rows)` — pyte's `Screen.resize(lines, columns)` takes `(rows, cols)`, so the args are swapped (the constructor `pyte.Screen(cols, rows)` is correct because pyte's `__init__` is `(columns, lines)`). `render/buffer.py:103` does it correctly. Any resize transposes the buffer.

- [ ] **Step 1: Read** `emulator.py` around 53 (constructor) and 178 (resize); confirm `render/buffer.py:103` as the correct reference.
- [ ] **Step 2: Write failing test:**

```python
def test_resize_preserves_geometry():
    emu = TerminalEmulator(cols=80, rows=24)
    emu.resize(120, 40)  # cols=120, rows=40
    snap = emu.snapshot()  # or whatever exposes geometry
    assert emu.cols == 120
    assert emu.rows == 40
    # write a full-width row and assert it is not truncated at 40
    emu.feed(b"x" * 120 + b"\r\n")
    assert len(emu.line(0).rstrip()) == 120
```

- [ ] **Step 3: Run, expect FAIL** (geometry transposed). `uv run pytest packages/provide-uterm/tests/ -k resize -v`
- [ ] **Step 4: Implement:**

```python
self._screen.resize(rows, cols)
```
- [ ] **Step 5: Run, expect PASS** + `uv run pytest packages/provide-uterm/tests/ -q`.
- [ ] **Step 6: Commit** — `fix(core): correct pyte resize argument order (rows, cols)`

---

### Task 2 (CORE-det 🟡): Make detector pattern mutation atomic

**Files:** Modify `packages/provide-uterm/src/provide/uterm/detection/detector.py:457-483` (`add_pattern`, `reload_patterns`). Test: detector test module.

**Problem:** Both methods mutate `self._patterns` BEFORE recompiling. In `strict=True`, `_compile_patterns()` raises `DetectorPatternCompileError` but `self._patterns` already holds the bad pattern → inconsistent state; every future call keeps re-raising on the poisoned list (permanent wedge).

- [ ] **Step 1: Read** `add_pattern`/`reload_patterns` (457-483) and `_compile_patterns` (note the strict-mode raise).
- [ ] **Step 2: Write failing test:**

```python
import pytest
def test_add_bad_pattern_strict_does_not_wedge_detector():
    det = PromptDetector(patterns=[{"name": "ok", "regex": "ready>"}], strict=True)
    with pytest.raises(DetectorPatternCompileError):
        det.add_pattern({"name": "bad", "regex": "("})  # invalid regex
    # State must be unchanged: the good pattern still works.
    assert det.pattern_count == 1
    det.add_pattern({"name": "ok2", "regex": "done>"})  # must succeed, not re-raise
    assert det.pattern_count == 2
```

- [ ] **Step 3: Run, expect FAIL.**
- [ ] **Step 4: Implement** compile-then-swap (build into locals from a candidate list, assign only on success):

```python
def add_pattern(self, pattern: dict[str, Any]) -> None:
    candidate = [*self._patterns, pattern]
    self._swap_patterns(candidate)

def reload_patterns(self, patterns: list[dict[str, Any]]) -> None:
    self._swap_patterns(list(patterns))

def _swap_patterns(self, candidate: list[dict[str, Any]]) -> None:
    saved = self._patterns
    self._patterns = candidate
    try:
        compiled_all = self._compile_patterns()
    except Exception:
        self._patterns = saved  # roll back before re-raising
        raise
    self._compiled_all = compiled_all
    self._compiled_no_cursor_end_req = [
        (regex, pat) for (regex, pat) in self._compiled_all if not bool(pat.get("expect_cursor_at_end", True))
    ]
    self._compiled = self._compiled_all
```
- [ ] **Step 5: Run, expect PASS** + suite green. (Detector is on the mutation perimeter — watch for survivors on the rollback branch.)
- [ ] **Step 6: Commit** — `fix(core): make detector pattern updates atomic (compile-then-swap)`

---

### Task 3 (CORE-iso 🟡): Reconcile memory vs sqlite transaction isolation

**Files:** Modify `packages/provide-uterm/src/provide/uterm/control/plane/memory/engine.py:45-46` and `memory/transaction.py:71-80`. Test: control-plane test module (run against BOTH backends).

**Problem:** The sqlite backend serializes via `BEGIN IMMEDIATE` + held `_tx_lock`; the memory backend snapshots `root` per-transaction and does last-writer-wins key merges with **no conflict detection**, yet both advertise `EngineCapabilities.supports_transactions=True`. A lease/owner race sqlite rejects can double-grant on memory.

- [ ] **Step 1: Read** `memory/engine.py` `begin()`, `memory/transaction.py` commit/merge, and the sqlite `_tx_lock` usage for the target semantics.
- [ ] **Step 2: Write failing test** (parametrized over both engines): two overlapping transactions both acquire the same lease; exactly one must win.

```python
import pytest
@pytest.mark.parametrize("engine", ["memory", "sqlite"], indirect=True)
async def test_concurrent_lease_acquire_single_winner(engine):
    async def acquire():
        async with engine.begin() as tx:
            store = engine.lease_store(tx)
            if await store.get_lease("w1") is None:
                await store.set_lease("w1", owner="me")
                return True
            return False
    results = await asyncio.gather(acquire(), acquire())
    assert sum(results) == 1  # currently can be 2 on memory
```

- [ ] **Step 3: Run, expect FAIL on the memory parametrization.**
- [ ] **Step 4: Implement.** Make memory `begin()` hold `self._lock` for the transaction's lifetime (serialize like sqlite), so overlapping transactions cannot both snapshot-then-merge. Confirm `supports_transactions=True` is now truthful. If serializing the whole transaction is too coarse for some callers, instead add optimistic-concurrency conflict detection on commit (compare snapshotted vs current for written keys, raise on conflict) — pick one and document it. Prefer the lock approach for parity simplicity.
- [ ] **Step 5: Run, expect PASS on both backends** + suite green.
- [ ] **Step 6: Commit** — `fix(core): serialize memory control-plane transactions to match sqlite isolation`

---

### Task 4 (CORE-ord 🟢): Align `list_pending` ordering across backends

**Files:** Modify `packages/provide-uterm/src/provide/uterm/control/plane/memory/approval_store.py:25-26`. Test: control-plane approval test module.

**Problem:** memory `list_pending` returns dict-insertion order; sqlite returns `ORDER BY created_at ASC, approval_id ASC`. FIFO consumers behave differently per backend.

- [ ] **Step 1: Read** both `list_pending` implementations.
- [ ] **Step 2: Write failing parametrized test:** insert approvals out of `created_at` order; assert `list_pending()` returns them sorted by `(created_at, approval_id)` on BOTH backends.
- [ ] **Step 3: Run, expect FAIL on memory.**
- [ ] **Step 4: Implement.** Sort the memory result: `sorted(pending, key=lambda a: (a.created_at, a.approval_id))`.
- [ ] **Step 5: Run, expect PASS** + suite green.
- [ ] **Step 6: Commit** — `fix(core): order memory list_pending by (created_at, approval_id) to match sqlite`

---

### Task 5 (CORE-id 🟢): Replace `id(ws)` anonymous identity with a per-connection UUID

**Files:** Modify `packages/provide-uterm/src/provide/uterm/deckmux/_service.py:106,143,161`. Test: deckmux test module.

**Problem:** Anonymous identity falls back to `str(id(ws))`; Python reuses `id()` after GC, so a new ws can collide with a disconnected one's id → mis-attributed presence/ownership.

- [ ] **Step 1: Read** the three `id(ws)` sites and how identity is keyed.
- [ ] **Step 2: Write failing test:** two distinct connection objects that (in a contrived reuse) share an `id` must still get distinct presence identities — or simpler, assert identity is derived from a stable per-connection token, not `id()`.
- [ ] **Step 3: Run, expect FAIL.**
- [ ] **Step 4: Implement.** Mint a `uuid4().hex` once per connection and stash it on the ws (e.g. `ws.state` / an attribute or a `WeakKeyDictionary[ws, str]`); use that everywhere instead of `id(ws)`. Keep the 30s idle prune.
- [ ] **Step 5: Run, expect PASS** + suite green.
- [ ] **Step 6: Commit** — `fix(core): use per-connection UUID for anonymous deckmux identity`

---

### Task 6 (CORE-cov 🟡): Close the coverage-perimeter gap

**Files:** Modify `packages/provide-uterm/pyproject.toml` (`[tool.coverage.run].source` and `[tool.coverage.report].partial_branches`). Add tests as needed under `packages/provide-uterm/tests/`.

**Problem:** Coverage is measured against an explicit module allowlist; `auth.py`, `recording.py`, `control_channel_builders.py`, `control_channel_patterns.py`, `ws_bytes.py`, and the entire `control/` subpackage are NOT in the 100% line/branch gate. Also two `partial_branches` regexes (esp. the broad `elif msg_type ==`) silently waive real branches.

> ⚠️ This task can surface many previously-unmeasured uncovered lines. Treat it as potentially the largest task in this lane. If the newly-included modules reveal substantial gaps, fix the high-value ones (`auth.py`, `control/plane/`) first and record the remainder as a follow-up rather than blocking the lane.

- [ ] **Step 1:** Switch `[tool.coverage.run].source` from the enumerated allowlist to the package root, adding an explicit `omit` for anything genuinely out-of-scope (egg-info, generated). Run `uv run pytest packages/provide-uterm/tests/ --cov --cov-report=term-missing -q` to see the new gap.
- [ ] **Step 2:** For each newly-uncovered branch in `auth.py` and `control/`, write a targeted test that exercises it (red→green). Do NOT paper over with `# pragma: no cover` unless a branch is provably unreachable, in which case use a per-site `# pragma: no cover` with a one-line justification comment.
- [ ] **Step 3:** Replace the `partial_branches` regex escape-hatches with per-site `# pragma: no branch` at the specific call sites (so the waiver is visible/grep-able), or add the missing branch tests. The broad `elif msg_type ==` exemption must go.
- [ ] **Step 4:** Confirm `--cov-fail-under=100` passes against the widened source set.
- [ ] **Step 5: Commit** — split sensibly: one commit widening `source` + its new tests, one commit removing the `partial_branches` hatches. (Multiple commits OK here per the one-logical-unit rule.)

---

### Done criteria (Lane A5)
- [ ] `uv run pytest packages/provide-uterm/tests/ -q` green at 100% over the widened perimeter
- [ ] `uv run ruff check --fix && uv run ruff format && uv run mypy packages/provide-uterm/src/`
- [ ] `uv run python scripts/run_mutation_gate.py --changed-only` → 0 survivors on touched perimeter files (detector, control-plane, schemas if touched)
- [ ] If any task changed `bridge/schemas.py`: `uv run python scripts/codegen_frames.py` and commit `schemas.py` + `frames.schema.json` + `frames.ts` together. (None of the tasks above require it, but verify.)
- [ ] Commits, one logical unit each.

### Cross-lane requests
_(record any out-of-scope change needed here)_
