# CM-05: Operator Input-Mode Lock Release in Go, C# and TypeScript

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the three ports the operator-lock release that Python has, so a
session cannot latch permanently into `hijack` after an operator sets a mode.

**Architecture:** All four implementations carry the same guard — a worker hello
may not lower a decided mode to `open`. Python additionally clears the decision
flag when a hello changes the mode, which is what lets a worker legitimately get
back to `open`. Go, C# and TypeScript set the mode and leave the flag set
forever. Add the release to each, and add the conformance scenario family that
none of the four designs anticipated.

**Tech Stack:** Go 1.26, C# .NET 10 / xUnit, TypeScript / vitest, Python /
pytest, shared JSON conformance scenarios.

## Global Constraints

- Python is the oracle for this behavior. Its implementation does not change.
- Each port's existing public API does not change. This is a state-transition
  fix inside the hello handler.
- SPDX headers on any new file, in each language's established comment form.
- Every port keeps its existing coverage floor: Python 100% branch,
  TypeScript 100% on all four metrics, C# its `make quality-gate` floor.
- Tests are written per-port first, then the shared scenario family is added.
  A port that only passes the shared runner but has no local regression test
  fails a future refactor silently.

## Context

Python, `packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/connection.py:240-258`:

```python
would_lower = mode == "open" and st.input_mode == "hijack"
if would_lower and (st.input_mode_set_by_operator or hub.is_hijacked(st)):
    logger.warning("worker_hello_mode_blocked worker_id=%s — ...", worker_id)
    return False
# The flag guards the *value* an operator chose, not the worker forever. ...
if mode != st.input_mode:
    st.input_mode_set_by_operator = False
st.input_mode = mode
```

The comment in the Python source explains the failure the release prevents:
without it, "a session ends up stuck in `hijack` when the operator and the
worker both asked for `open`."

The three ports carry the guard and not the release:

| Port | Guard | Release |
|---|---|---|
| Go | `hub/connection.go:167` | absent — `st.InputMode = mode` at `:173` |
| C# | `Hub/Connection.cs:351` | absent — `st.InputMode = mode` at `:354` |
| TypeScript | `src/hub/connection.ts:251` | absent — `state.inputMode = mode` at `:254` |

The consequence: once an operator sets a mode, `inputModeSetByOperator` is true
for the lifetime of the registry entry. A hello that raises the mode over a
decided `open` has already overridden the operator's decision, so the mode on
the state is the worker's — but the flag still claims otherwise, and every
subsequent attempt to lower to `open` is refused. Operator and worker can both
be asking for `open` and neither gets it.

There is a second, smaller divergence in the same expression. Go's guard reads:

```go
if wouldLower && (st.InputModeSetByOperator || hub.State.IsHijacked(st) || st.HijackPending != nil) {
```

The `st.HijackPending != nil` term has no counterpart in Python, C# or
TypeScript. Task 4 resolves it by fixture rather than by assuming Go is wrong.

Measured 2026-08-03; see
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`,
finding CM-05.

## File Structure

- `packages/provide-uterm-go/hub/connection.go` — add the release.
- `packages/provide-uterm-go/hub/connection_test.go` — regression test.
- `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Connection.cs` — add the release.
- `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/HubInputModeLockTests.cs` — new.
- `packages/provide-uterm-ts/src/hub/connection.ts` — add the release.
- `packages/provide-uterm-ts/src/hub/connection.test.ts` — regression test.
- `spec/input_mode_lifecycle_scenarios.json` — new scenario family.
- `tests/conformance/test_input_mode_parity.py` — new runner.

Each port is its own task so a reviewer can reject one without blocking the
others.

---

### Task 1: Go releases the lock when a hello changes the mode

**Files:**
- Modify: `packages/provide-uterm-go/hub/connection.go:173`
- Modify: `packages/provide-uterm-go/hub/connection_test.go`

**Interfaces:**
- Consumes: existing `SessionState.InputMode`, `SessionState.InputModeSetByOperator`.
- Produces: no signature change.

- [ ] **Step 1: Write the failing test**

Add to `packages/provide-uterm-go/hub/connection_test.go`. Match the file's
existing setup helpers rather than inventing new ones — read it first and reuse
whatever it uses to construct a hub and a registered worker.

```go
func TestWorkerHelloReleasesOperatorLockWhenModeChanges(t *testing.T) {
	// The stuck-session shape: an operator decides `open`, a worker hello
	// raises to `hijack` — which overrides that decision, so the mode on the
	// state is now the worker's — and then the worker asks for `open` again.
	// Leaving the flag set refuses it forever, so operator and worker can both
	// want `open` and neither gets it.
	hub, workerID := newTestHubWithWorker(t)

	st := hub.State.Get(workerID)
	st.InputMode = InputModeOpen
	st.InputModeSetByOperator = true

	ok, err := hub.Connection.WorkerHello(context.Background(), workerID, InputModeHijack, nil)
	if err != nil {
		t.Fatalf("hello raising to hijack: %v", err)
	}
	if !ok {
		t.Fatal("a hello raising the mode over a decided open must be accepted")
	}
	if st.InputModeSetByOperator {
		t.Error("the operator decision was already overridden; the flag must not still claim it stands")
	}

	ok, err = hub.Connection.WorkerHello(context.Background(), workerID, InputModeOpen, nil)
	if err != nil {
		t.Fatalf("hello lowering back to open: %v", err)
	}
	if !ok {
		t.Fatal("the worker cannot reach open again: the session is stuck in hijack")
	}
	if st.InputMode != InputModeOpen {
		t.Errorf("InputMode = %q, want %q", st.InputMode, InputModeOpen)
	}
}

func TestWorkerHelloAgreeingWithModeLeavesDecisionStanding(t *testing.T) {
	// A hello that changes nothing must not clear the flag. Otherwise a worker
	// could launder away an operator's decision just by repeating it.
	hub, workerID := newTestHubWithWorker(t)

	st := hub.State.Get(workerID)
	st.InputMode = InputModeHijack
	st.InputModeSetByOperator = true

	ok, err := hub.Connection.WorkerHello(context.Background(), workerID, InputModeHijack, nil)
	if err != nil {
		t.Fatalf("hello agreeing with the mode: %v", err)
	}
	if !ok {
		t.Fatal("a hello agreeing with the current mode must be accepted")
	}
	if !st.InputModeSetByOperator {
		t.Error("a hello that changed nothing cleared the operator decision")
	}
}
```

Replace `newTestHubWithWorker` and `hub.Connection.WorkerHello` with whatever
the existing tests in that file actually use — the names above describe the
shape, not necessarily the current API.

- [ ] **Step 2: Run the tests to verify the first fails**

Run: `cd packages/provide-uterm-go && go test ./hub/ -run TestWorkerHelloReleases -v`

Expected: FAIL at "the operator decision was already overridden" — the flag is
still true after the raising hello. The second test passes already and is
regression cover for what the release must not break.

- [ ] **Step 3: Add the release**

In `hub/connection.go`, replace `st.InputMode = mode` with:

```go
		// The flag guards the value an operator chose, not the worker forever.
		// A hello that raises over a decided `open` has already overridden that
		// decision — the mode on the state is the worker's now — so leaving the
		// flag set would refuse the worker its own way back to `open` for the
		// lifetime of the registry entry. That is how a session ends up stuck
		// in hijack with the operator and the worker both asking for open. A
		// hello that agrees with the current mode changes nothing and leaves
		// the decision standing.
		if mode != st.InputMode {
			st.InputModeSetByOperator = false
		}
		st.InputMode = mode
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd packages/provide-uterm-go && go test -race ./hub/ -v`

Expected: PASS, including the pre-existing hub tests.

- [ ] **Step 5: Commit**

```bash
git add packages/provide-uterm-go/hub/connection.go packages/provide-uterm-go/hub/connection_test.go
git commit -m "fix(go): release the operator input-mode lock when a hello changes the mode

The guard refuses a hello that would lower a decided mode to open. What
was missing is the other half: a hello that *raises* over a decided open
has already overridden the operator's decision, so the mode on the state
is the worker's — but the flag still claimed otherwise, and every later
attempt to reach open was refused for the lifetime of the registry
entry.

Operator and worker could both be asking for open and neither would get
it. Python has had the release since the incident that produced the
guard; Go carried the guard alone."
```

---

### Task 2: C# releases the lock when a hello changes the mode

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Connection.cs:354`
- Create: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/HubInputModeLockTests.cs`

**Interfaces:**
- Consumes: existing `SessionState.InputMode`, `SessionState.InputModeSetByOperator`.
- Produces: no signature change.

- [ ] **Step 1: Write the failing test**

Create `tests/Provide.Uterm.Tests/HubInputModeLockTests.cs`:

```csharp
//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;

namespace Provide.Uterm.Tests;

/// <summary>
/// The operator input-mode decision flag guards a value, not a worker. These
/// tests pin when it is released, which is the half the port was missing.
/// </summary>
public class HubInputModeLockTests
{
    [Fact]
    public async Task WorkerHello_RaisingOverADecidedOpen_ReleasesTheLock()
    {
        var (hub, workerId, state) = await NewHubWithWorkerAsync();
        state.InputMode = InputModes.Open;
        state.InputModeSetByOperator = true;

        var (accepted, _) = await hub.Connection.WorkerHelloAsync(workerId, InputModes.Hijack, null);

        Assert.True(accepted);
        Assert.False(state.InputModeSetByOperator);
    }

    [Fact]
    public async Task WorkerHello_AfterRaising_CanReachOpenAgain()
    {
        var (hub, workerId, state) = await NewHubWithWorkerAsync();
        state.InputMode = InputModes.Open;
        state.InputModeSetByOperator = true;

        await hub.Connection.WorkerHelloAsync(workerId, InputModes.Hijack, null);
        var (accepted, _) = await hub.Connection.WorkerHelloAsync(workerId, InputModes.Open, null);

        Assert.True(accepted);
        Assert.Equal(InputModes.Open, state.InputMode);
    }

    [Fact]
    public async Task WorkerHello_AgreeingWithTheMode_LeavesTheDecisionStanding()
    {
        var (hub, workerId, state) = await NewHubWithWorkerAsync();
        state.InputMode = InputModes.Hijack;
        state.InputModeSetByOperator = true;

        var (accepted, _) = await hub.Connection.WorkerHelloAsync(workerId, InputModes.Hijack, null);

        Assert.True(accepted);
        Assert.True(state.InputModeSetByOperator);
    }
}
```

Add a `NewHubWithWorkerAsync` helper to this file following the setup used by
the existing hub tests — read
`packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/BrowserWsRateLimitTests.cs`
for how a hub and a registered worker are constructed there, and reuse it rather
than inventing a second pattern. Adjust `WorkerHelloAsync`'s actual signature to
match `Connection.cs`.

- [ ] **Step 2: Run the tests to verify two fail**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet test tests/Provide.Uterm.Tests --filter FullyQualifiedName~HubInputModeLockTests
```

Expected: `RaisingOverADecidedOpen_ReleasesTheLock` and
`AfterRaising_CanReachOpenAgain` FAIL. `AgreeingWithTheMode` passes and is
regression cover.

- [ ] **Step 3: Add the release**

In `Hub/Connection.cs`, inside the `if (!blocked)` block, replace
`st.InputMode = mode;` with:

```csharp
                // The flag guards the value an operator chose, not the worker
                // forever. A hello that raises over a decided `open` has
                // already overridden that decision — the mode on the state is
                // the worker's now — so leaving the flag set would refuse the
                // worker its own way back to `open` for the lifetime of the
                // registry entry. A hello that agrees with the current mode
                // changes nothing and leaves the decision standing.
                if (mode != st.InputMode)
                {
                    st.InputModeSetByOperator = false;
                }

                st.InputMode = mode;
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet test tests/Provide.Uterm.Tests --filter FullyQualifiedName~HubInputModeLockTests
dotnet test tests/Provide.Uterm.Tests
```

Expected: PASS, including the full suite.

- [ ] **Step 5: Commit**

```bash
git add packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Connection.cs \
        packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/HubInputModeLockTests.cs
git commit -m "fix(csharp): release the operator input-mode lock when a hello changes the mode

Same missing half as the Go port: the guard refusing a lowering hello
was there, the release was not, so InputModeSetByOperator stayed true
for the lifetime of the registry entry and a session that had been
raised to hijack could never be lowered back to open."
```

---

### Task 3: TypeScript releases the lock when a hello changes the mode

**Files:**
- Modify: `packages/provide-uterm-ts/src/hub/connection.ts:254`
- Modify: `packages/provide-uterm-ts/src/hub/connection.test.ts`

**Interfaces:**
- Consumes: existing `state.inputMode`, `state.inputModeSetByOperator`.
- Produces: no signature change.

- [ ] **Step 1: Write the failing test**

Add to `packages/provide-uterm-ts/src/hub/connection.test.ts`, following the
setup helpers already in that file:

```typescript
  it("releases the operator lock when a hello changes the mode", () => {
    // A hello that raises over a decided `open` has already overridden the
    // operator's decision. Leaving the flag set refuses the worker its own way
    // back to `open` for the lifetime of the registry entry.
    const { hub, workerId, state } = newHubWithWorker();
    state.inputMode = "open";
    state.inputModeSetByOperator = true;

    expect(hub.connection.workerHello(workerId, "hijack", undefined)).toBe(true);
    expect(state.inputModeSetByOperator).toBe(false);

    expect(hub.connection.workerHello(workerId, "open", undefined)).toBe(true);
    expect(state.inputMode).toBe("open");
  });

  it("leaves the operator decision standing when a hello agrees with the mode", () => {
    const { hub, workerId, state } = newHubWithWorker();
    state.inputMode = "hijack";
    state.inputModeSetByOperator = true;

    expect(hub.connection.workerHello(workerId, "hijack", undefined)).toBe(true);
    expect(state.inputModeSetByOperator).toBe(true);
  });
```

Replace `newHubWithWorker` and the `workerHello` call shape with what the file
already uses.

- [ ] **Step 2: Run the tests to verify the first fails**

Run:
```bash
cd packages/provide-uterm-ts
npx vitest run src/hub/connection.test.ts
```

Expected: FAIL — `inputModeSetByOperator` is still `true` after the raising
hello, so the second `workerHello` returns `false`.

- [ ] **Step 3: Add the release**

In `src/hub/connection.ts`, replace `state.inputMode = mode;` with:

```typescript
    // The flag guards the value an operator chose, not the worker forever. A
    // hello that raises over a decided `open` has already overridden that
    // decision — the mode on the state is the worker's now — so leaving the
    // flag set would refuse the worker its own way back to `open` for the
    // lifetime of the registry entry. A hello that agrees with the current
    // mode changes nothing and leaves the decision standing.
    if (mode !== state.inputMode) {
      state.inputModeSetByOperator = false;
    }
    state.inputMode = mode;
```

- [ ] **Step 4: Run the tests and the coverage gate**

Run:
```bash
cd packages/provide-uterm-ts
npx vitest run src/hub/connection.test.ts
npm run typecheck
npm run test:coverage
```

Expected: PASS, with all four coverage metrics still at 100%. The new branch is
covered by both tests together — one takes it, one does not.

- [ ] **Step 5: Commit**

```bash
git add packages/provide-uterm-ts/src/hub/connection.ts \
        packages/provide-uterm-ts/src/hub/connection.test.ts
git commit -m "fix(ts): release the operator input-mode lock when a hello changes the mode

Third port with the guard and without the release. The flag stayed true
for the lifetime of the registry entry, so a session raised to hijack
could never be lowered back to open even when the operator and the
worker both wanted it."
```

---

### Task 4: Shared input-mode lifecycle scenarios, and the Go guard divergence

**Files:**
- Create: `spec/input_mode_lifecycle_scenarios.json`
- Create: `tests/conformance/test_input_mode_parity.py`
- Possibly modify: `packages/provide-uterm-go/hub/connection.go:167`

**Interfaces:**
- Consumes: the scenario-runner conventions in `tests/conformance/backends.py`.
- Produces: `spec/input_mode_lifecycle_scenarios.json`.

None of the four 2026-08-02 designs anticipated this family. It exists because
the measurement found a divergence the designs did not describe, which is itself
worth noting: the six families they did name were not a complete list.

- [ ] **Step 1: Write the scenario file**

Read `spec/session_lifecycle_security_scenarios.json` first and follow its
schema. Seven scenarios:

| Scenario ID | Setup | Hello | Expected |
|---|---|---|---|
| `inputmode_001_hello_may_raise` | mode `open`, operator-set | `hijack` | accepted, mode `hijack`, flag cleared |
| `inputmode_002_hello_may_not_lower_decided` | mode `hijack`, operator-set | `open` | refused, mode unchanged, flag still set |
| `inputmode_003_agreeing_hello_keeps_decision` | mode `hijack`, operator-set | `hijack` | accepted, mode unchanged, flag still set |
| `inputmode_004_lower_allowed_after_release` | mode `open`, operator-set, then hello `hijack` | `open` | accepted, mode `open` |
| `inputmode_005_hello_may_not_lower_while_hijacked` | mode `hijack`, not operator-set, session hijacked | `open` | refused |
| `inputmode_006_undecided_lower_allowed` | mode `hijack`, not operator-set, not hijacked | `open` | accepted |
| `inputmode_007_lower_while_hijack_pending` | mode `hijack`, not operator-set, hijack pending, not yet hijacked | `open` | **see Step 3** |

- [ ] **Step 2: Run scenarios 001-006 against all four backends**

Run: `uv run pytest tests/conformance/test_input_mode_parity.py -v -k "not 007"`

Expected: PASS on Python, Go, C# and TypeScript. Scenario `004` is the one that
would have failed on the three ports before Tasks 1-3.

- [ ] **Step 3: Resolve the Go guard divergence with scenario 007**

Go's guard has an extra term the other three lack:

```go
if wouldLower && (st.InputModeSetByOperator || hub.State.IsHijacked(st) || st.HijackPending != nil) {
```

Scenario `007` is exactly the state that term covers and the others do not: a
hijack requested but not yet established. Run it and observe what each backend
does:

Run: `uv run pytest tests/conformance/test_input_mode_parity.py -v -k "007"`

Expected: Go refuses, Python/C#/TypeScript accept. That is a real disagreement
about behavior, not a bug in one port, and the fixture is what decides it.

Decide deliberately and write the decision into the scenario file as a comment:

- If refusing is correct — a pending hijack is a decision in flight and lowering
  the mode out from under it reproduces the original incident — then Python is
  the one to change, and the design docs say so explicitly: "If a fixture
  exposes a defect in Python, the contract is corrected and all served
  implementations converge; parity never means reproducing a known bug."
- If accepting is correct, remove the term from Go.

Do not pick whichever needs less work. Read
`packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/connection.py:225-245`
and the incident the comment there describes before deciding.

- [ ] **Step 4: Implement the decision and re-run**

Run: `uv run pytest tests/conformance/test_input_mode_parity.py -v`

Expected: PASS on all four backends, all seven scenarios.

- [ ] **Step 5: Commit**

```bash
git add spec/input_mode_lifecycle_scenarios.json tests/conformance/test_input_mode_parity.py
git commit -m "test(conformance): shared input-mode lifecycle scenarios

Seven scenarios pinning when a worker hello may raise, may lower, and
when the operator decision is released. Scenario 004 is the one the
three ports failed before this wave.

Scenario 007 covers a hijack pending but not established — the one state
where Go's guard has a term the other three lack. The disagreement is
settled by the fixture rather than by assuming the majority is right.

None of the 2026-08-02 designs named this family, which is worth
recording: the six they did name were not a complete list."
```

---

## Definition of done

Per the measurement spec, CM-05 closes when:

- `spec/input_mode_lifecycle_scenarios.json` passes on all four backends;
- scenario `inputmode_004_lower_allowed_after_release` was observed failing on
  Go, C# and TypeScript before Tasks 1-3;
- each port has a local regression test as well as the shared scenario;
- the Go guard divergence is resolved in one direction with the reasoning
  recorded in the scenario file;
- each port's own gate passes: `go test -race ./...`,
  `dotnet test` plus `make quality-gate`, and `npm run test:coverage` at 100%.

Then update the CM-05 rows and the Status date in
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`.

## Notes for the implementer

- The release condition is `mode != current`, not `mode == "open"`. Clearing the
  flag only when lowering would leave it set after a raise, which is the exact
  state that produces the stuck session.
- Do not clear the flag inside the `blocked` branch. A refused hello changed
  nothing and must not launder away the operator's decision — that would turn
  the guard into a one-shot.
- Python's comment at `connection.py:246-255` is the best available description
  of why this works the way it does. Read it before editing any port, and keep
  the ported comments faithful to it rather than paraphrasing away the reason.
- Python also carries a note about a coverage.py 3.11 `async with` `__aexit__`
  arc quirk immediately after this code. It is unrelated to this change; leave
  it alone.
