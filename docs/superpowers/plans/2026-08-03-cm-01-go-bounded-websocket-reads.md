# CM-01: Bounded Go WebSocket Reads

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the five `SetReadLimit(-1)` calls in the Go client paths, so a
hostile or compromised peer cannot drive unbounded allocation with a single
oversized WebSocket message.

**Architecture:** The Go server paths already bound reads at
`hub.MaxWSMessageBytes()` (1 MiB). The client paths explicitly disable the limit
instead. Introduce one shared helper that validates a configured bound and
applies it, put the 1 MiB constant in the existing `defaults` package where the
repo keeps such values, and route all five call sites plus the four existing
server sites through it.

**Tech Stack:** Go 1.26, `github.com/coder/websocket` v1.8.15, standard
`testing` package.

## Global Constraints

- Go 1.26.5 (`packages/provide-uterm-go/go.mod`). The module is standalone — it
  is not part of the uv or npm workspaces and has its own CI.
- All new files carry SPDX headers:
  ```go
  //
  // SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
  // SPDX-License-Identifier: AGPL-3.0-or-later
  //
  ```
- No hardcoded limits at call sites. Per repo convention, shared numeric
  defaults live in `packages/provide-uterm-go/defaults/defaults.go` and are
  asserted in `defaults/defaults_test.go`.
- `gofmt`, `go vet`, and `go test -race` must all pass.
- The wire format does not change. This bounds what is accepted, not what is
  sent.

## Context

Measured 2026-08-03:

```
$ grep -rn "SetReadLimit" packages/provide-uterm-go --include="*.go" | grep -v _test
bridge/worker_link.go:371   conn.SetReadLimit(int64(b.maxWSMessageBytes))     bounded
server/ws_worker.go:59      conn.SetReadLimit(int64(s.deps.Hub.MaxWSMessageBytes()))  bounded
server/ws_browser.go:196    conn.SetReadLimit(int64(s.deps.Hub.MaxWSMessageBytes()))  bounded
server/ws_tunnel.go:48      conn.SetReadLimit(int64(s.deps.Hub.MaxWSMessageBytes()))  bounded
vnc/litevirt_human.go:204   c.SetReadLimit(1 << 20)                          bounded, inline
transports/websocket.go:76  conn.SetReadLimit(-1)                            UNBOUNDED
tunnelclient/client.go:71   conn.SetReadLimit(-1)                            UNBOUNDED
cli/watch.go:134            conn.SetReadLimit(-1)                            UNBOUNDED
client/controlws.go:173     conn.SetReadLimit(-1)                            UNBOUNDED
gateway/pump.go:169         conn.SetReadLimit(-1)                            UNBOUNDED
```

Two of the unbounded sites carry a comment explaining the intent — `transports/websocket.go:76`
says "terminal frames can be large; disable the 32 KiB cap." That reasoning is
sound about the library default and wrong about the remedy: `coder/websocket`
defaults to 32 KiB, which is genuinely too small for a terminal frame, but the
fix is a larger bound, not no bound.

These are client paths, so the hostile party is a compromised or malicious
server, tunnel peer, or gateway upstream rather than an arbitrary caller. That
narrows who can trigger it; it does not make an unbounded read safe.

`coder/websocket` already implements the behavior the design asks for once a
limit is set: it accumulates fragments against the limit, fails the read before
allocating past it, and closes with `StatusMessageTooBig`. The work here is
applying a limit consistently and validating it, not writing a new reader.

Measured 2026-08-03; see
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`,
finding CM-01.

## File Structure

- `defaults/defaults.go` — add `MaxWSMessageBytes`. This is where the repo keeps
  shared numeric defaults, and the constant is currently duplicated as a literal
  in `hub/termhub.go:197`, `bridge/worker_link.go:167`,
  `controlchannel/codec.go:31`, and `vnc/litevirt_human.go:204`.
- `wslimit/wslimit.go` — new package. One exported function that validates and
  applies a bound. A package rather than a helper inside `transports` because
  five unrelated packages need it and none of them should import another.
- `wslimit/wslimit_test.go` — new.
- Five call sites modified, listed in Task 3.

---

### Task 1: Shared limit constant and validated application

**Files:**
- Modify: `packages/provide-uterm-go/defaults/defaults.go`
- Modify: `packages/provide-uterm-go/defaults/defaults_test.go`
- Create: `packages/provide-uterm-go/wslimit/wslimit.go`
- Create: `packages/provide-uterm-go/wslimit/wslimit_test.go`

**Interfaces:**
- Produces:
  ```go
  // package defaults
  const MaxWSMessageBytes = 1_048_576

  // package wslimit
  const MinBytes = 1024
  func Apply(conn *websocket.Conn, maxBytes int) error
  func Resolve(configured int) (int, error)
  ```
  `Resolve` maps 0 to the default and rejects anything else below `MinBytes`.
  `Apply` calls `Resolve` and then `SetReadLimit`. Tasks 2 and 3 consume both.

- [ ] **Step 1: Write the failing test**

Create `wslimit/wslimit_test.go`:

```go
//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package wslimit

import (
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/defaults"
)

func TestResolveZeroUsesDefault(t *testing.T) {
	got, err := Resolve(0)
	if err != nil {
		t.Fatalf("Resolve(0) returned error: %v", err)
	}
	if got != defaults.MaxWSMessageBytes {
		t.Errorf("Resolve(0) = %d, want %d", got, defaults.MaxWSMessageBytes)
	}
}

func TestResolveAcceptsExplicitRaise(t *testing.T) {
	// A caller may raise the bound deliberately; that is configuration, not a
	// bypass. What they may not do is remove it.
	got, err := Resolve(8 << 20)
	if err != nil {
		t.Fatalf("Resolve(8MiB) returned error: %v", err)
	}
	if got != 8<<20 {
		t.Errorf("Resolve(8MiB) = %d, want %d", got, 8<<20)
	}
}

func TestResolveRejectsNonsense(t *testing.T) {
	// -1 is the value every one of the unbounded call sites passed to
	// SetReadLimit. It must not survive validation.
	for _, configured := range []int{-1, 1, 1023, -1 << 40} {
		if _, err := Resolve(configured); err == nil {
			t.Errorf("Resolve(%d) accepted a limit below the floor", configured)
		}
	}
}

func TestResolveAcceptsExactFloor(t *testing.T) {
	got, err := Resolve(MinBytes)
	if err != nil {
		t.Fatalf("Resolve(MinBytes) returned error: %v", err)
	}
	if got != MinBytes {
		t.Errorf("Resolve(MinBytes) = %d, want %d", got, MinBytes)
	}
}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd packages/provide-uterm-go && go test ./wslimit/`

Expected: FAIL — the package does not exist.

- [ ] **Step 3: Add the constant**

In `defaults/defaults.go`, add to the constant block:

```go
	// MaxWSMessageBytes bounds a single inbound WebSocket message. The server
	// paths have always used this value; the client paths disabled the limit
	// entirely, which is what this constant exists to stop repeating.
	MaxWSMessageBytes = 1_048_576
```

In `defaults/defaults_test.go`, add to the `intConsts` table:

```go
		{"MaxWSMessageBytes", MaxWSMessageBytes, 1_048_576},
```

- [ ] **Step 4: Write the implementation**

Create `wslimit/wslimit.go`:

```go
//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package wslimit applies one bound to every WebSocket read path.
//
// coder/websocket defaults to a 32 KiB read limit, which is too small for a
// terminal frame. Several client paths responded by calling SetReadLimit(-1),
// which removes the bound rather than raising it, so one oversized message from
// a compromised peer could drive an unbounded allocation. The library already
// does the right thing once a limit is set — it accumulates fragments against
// the limit, fails before allocating past it, and closes with
// StatusMessageTooBig — so the only thing missing was applying one.
package wslimit

import (
	"fmt"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/defaults"
)

// MinBytes is the smallest bound worth accepting. Below this a caller has
// almost certainly made a mistake, and a limit small enough to reject ordinary
// control frames is its own outage.
const MinBytes = 1024

// Resolve validates a configured bound. Zero means "unset" and yields the
// default; anything else must be at least MinBytes. Negative values are
// rejected explicitly because -1 is exactly what the unbounded call sites used.
func Resolve(configured int) (int, error) {
	if configured == 0 {
		return defaults.MaxWSMessageBytes, nil
	}
	if configured < MinBytes {
		return 0, fmt.Errorf("websocket read limit %d is below the %d-byte floor", configured, MinBytes)
	}
	return configured, nil
}

// Apply resolves the bound and installs it on conn.
func Apply(conn *websocket.Conn, maxBytes int) error {
	resolved, err := Resolve(maxBytes)
	if err != nil {
		return err
	}
	conn.SetReadLimit(int64(resolved))
	return nil
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd packages/provide-uterm-go && go test ./wslimit/ ./defaults/`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/provide-uterm-go/wslimit/ packages/provide-uterm-go/defaults/
git commit -m "feat(go): add a validated shared WebSocket read bound

coder/websocket defaults to 32 KiB, which is too small for a terminal
frame, and several client paths answered that by calling
SetReadLimit(-1) — removing the bound instead of raising it.

Add one place that resolves and validates a bound, with the 1 MiB
default alongside the other shared constants. Zero means unset; anything
below the floor is refused, including the -1 those call sites used."
```

---

### Task 2: An over-limit message is refused rather than allocated

**Files:**
- Modify: `packages/provide-uterm-go/wslimit/wslimit_test.go`

**Interfaces:**
- Consumes: `wslimit.Apply` from Task 1.
- Produces: nothing new. This task proves the bound has the effect claimed.

Task 1 tested the arithmetic. This one tests the guarantee — that an oversized
message actually fails the read — because a limit that is set but not enforced
would pass every test in Task 1.

- [ ] **Step 1: Write the failing test**

Add to `wslimit/wslimit_test.go`:

```go
func TestApplyRefusesAnOverLimitMessage(t *testing.T) {
	// A server that sends more than the client agreed to accept. The read must
	// fail rather than allocate the whole thing.
	const limit = 4096

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer conn.CloseNow()
		_ = conn.Write(r.Context(), websocket.MessageBinary, make([]byte, limit*4))
		<-r.Context().Done()
	}))
	defer srv.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	conn, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(srv.URL, "http"), nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.CloseNow()

	if err := Apply(conn, limit); err != nil {
		t.Fatalf("Apply: %v", err)
	}

	if _, _, err := conn.Read(ctx); err == nil {
		t.Fatal("read accepted a message four times the configured limit")
	}
}

func TestApplyAcceptsAnExactLimitMessage(t *testing.T) {
	// The boundary is inclusive: a message of exactly the limit is legal, and a
	// bound that rejected it would break ordinary large frames.
	const limit = 4096

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer conn.CloseNow()
		_ = conn.Write(r.Context(), websocket.MessageBinary, make([]byte, limit))
		<-r.Context().Done()
	}))
	defer srv.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	conn, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(srv.URL, "http"), nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.CloseNow()

	if err := Apply(conn, limit); err != nil {
		t.Fatalf("Apply: %v", err)
	}

	_, payload, err := conn.Read(ctx)
	if err != nil {
		t.Fatalf("read refused a message of exactly the limit: %v", err)
	}
	if len(payload) != limit {
		t.Errorf("read %d bytes, want %d", len(payload), limit)
	}
}
```

Add these imports to the file's import block:

```go
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"time"
```

- [ ] **Step 2: Run the tests**

Run: `cd packages/provide-uterm-go && go test ./wslimit/ -run TestApply -v`

Expected: PASS. Both tests exercise the library's enforcement rather than our
own code, which is the point — this is the evidence that delegating to
`SetReadLimit` is sufficient.

- [ ] **Step 3: Prove the test would catch the defect**

Temporarily change `Apply` to mimic the old behavior:

```go
	conn.SetReadLimit(-1)
	return nil
```

Run: `cd packages/provide-uterm-go && go test ./wslimit/ -run TestApplyRefuses -v`

Expected: FAIL — the read succeeds and returns the oversized payload. This is
the pre-fix behavior of all five call sites.

Revert: `git checkout packages/provide-uterm-go/wslimit/wslimit.go`

Run the test again and confirm it passes.

- [ ] **Step 4: Commit**

```bash
git add packages/provide-uterm-go/wslimit/wslimit_test.go
git commit -m "test(go): prove the read bound refuses an over-limit message

Task 1 tested the arithmetic, which a limit that is set but never
enforced would also pass. Stand up a real server that sends four times
the configured limit and assert the read fails, plus the inclusive
boundary case so the bound does not reject ordinary large frames.

Verified by reinstating SetReadLimit(-1) and watching the refusal test
go red."
```

---

### Task 3: Route all five unbounded call sites through the helper

**Files:**
- Modify: `packages/provide-uterm-go/transports/websocket.go:76`
- Modify: `packages/provide-uterm-go/tunnelclient/client.go:71`
- Modify: `packages/provide-uterm-go/cli/watch.go:134`
- Modify: `packages/provide-uterm-go/client/controlws.go:173`
- Modify: `packages/provide-uterm-go/gateway/pump.go:169`

**Interfaces:**
- Consumes: `wslimit.Apply` from Task 1.
- Produces: nothing new.

Line numbers are as measured on 2026-08-03; locate by searching for
`SetReadLimit(-1)` if they have moved.

- [ ] **Step 1: Confirm the call sites**

Run:
```bash
cd packages/provide-uterm-go
grep -rn "SetReadLimit(-1)" --include="*.go" .
```

Expected: exactly the five sites above.

- [ ] **Step 2: Replace each call site**

At each of the five, replace:

```go
	conn.SetReadLimit(-1) // comment varies per site
```

with:

```go
	if err := wslimit.Apply(conn, 0); err != nil {
		return fmt.Errorf("apply websocket read limit: %w", err)
	}
```

and add the import:

```go
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/wslimit"
```

Two sites need care:

- `cli/watch.go:134` and `gateway/pump.go:169` may sit in functions that do not
  return an error. Check each signature before editing. If a site cannot return,
  log at error level and close the connection with
  `websocket.StatusInternalError` rather than ignoring the error — a bound that
  silently failed to apply is the defect this plan is closing.
- `transports/websocket.go:76` carries the comment "terminal frames can be
  large; disable the 32 KiB cap." Delete it. The observation was right and the
  remedy was wrong, and leaving it invites the next person to restore `-1`.

- [ ] **Step 3: Verify no unbounded site remains**

Run:
```bash
cd packages/provide-uterm-go
grep -rn "SetReadLimit(-1)" --include="*.go" .
```

Expected: no output.

- [ ] **Step 4: Build, vet, and test with the race detector**

Run:
```bash
cd packages/provide-uterm-go
gofmt -l .
go vet ./...
go test -race ./...
```

Expected: `gofmt -l` prints nothing, vet is clean, all tests pass.

- [ ] **Step 5: Commit**

```bash
git add packages/provide-uterm-go/transports/websocket.go \
        packages/provide-uterm-go/tunnelclient/client.go \
        packages/provide-uterm-go/cli/watch.go \
        packages/provide-uterm-go/client/controlws.go \
        packages/provide-uterm-go/gateway/pump.go
git commit -m "fix(go): bound reads on the five WebSocket client paths

The transport, tunnel client, watch, control client and gateway pump all
called SetReadLimit(-1), so one oversized message from a compromised
server or tunnel peer could drive an unbounded allocation. The server
paths had been bounded at 1 MiB the whole time.

Route all five through the shared helper. The 'terminal frames can be
large' comment goes with them: the observation was right, the remedy was
not, and leaving it invites restoring -1."
```

---

### Task 4: Server sites use the same helper

**Files:**
- Modify: `packages/provide-uterm-go/server/ws_worker.go:59`
- Modify: `packages/provide-uterm-go/server/ws_browser.go:196`
- Modify: `packages/provide-uterm-go/server/ws_tunnel.go:48`
- Modify: `packages/provide-uterm-go/bridge/worker_link.go:371`
- Modify: `packages/provide-uterm-go/vnc/litevirt_human.go:204`

**Interfaces:**
- Consumes: `wslimit.Apply` from Task 1.
- Produces: nothing new.

The server sites are already bounded and are not defective. They are routed
through the helper anyway so that `SetReadLimit` appears in exactly one place —
otherwise the next unbounded call site is one `grep` miss away, which is how
five of them accumulated.

- [ ] **Step 1: Replace the three server sites**

At `server/ws_worker.go:59`, `server/ws_browser.go:196` and
`server/ws_tunnel.go:48`, replace:

```go
	conn.SetReadLimit(int64(s.deps.Hub.MaxWSMessageBytes()))
```

with:

```go
	if err := wslimit.Apply(conn, s.deps.Hub.MaxWSMessageBytes()); err != nil {
		_ = conn.Close(websocket.StatusInternalError, "invalid read limit")
		return
	}
```

- [ ] **Step 2: Replace the bridge site**

At `bridge/worker_link.go:371`, replace
`conn.SetReadLimit(int64(b.maxWSMessageBytes))` with the `wslimit.Apply`
equivalent, propagating the error the way the surrounding function already
handles failures.

- [ ] **Step 3: Replace the VNC site**

At `vnc/litevirt_human.go:204`, replace `c.SetReadLimit(1 << 20) // 1 MiB` with
`wslimit.Apply(c, 0)`, handling the error as the surrounding function does. The
inline `1 << 20` was a fourth copy of the same number.

- [ ] **Step 4: Verify SetReadLimit appears once**

Run:
```bash
cd packages/provide-uterm-go
grep -rn "SetReadLimit" --include="*.go" . | grep -v _test
```

Expected: one hit, in `wslimit/wslimit.go`.

- [ ] **Step 5: Full verification**

Run:
```bash
cd packages/provide-uterm-go
gofmt -l .
go vet ./...
go test -race ./...
```

Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add packages/provide-uterm-go/server/ packages/provide-uterm-go/bridge/worker_link.go \
        packages/provide-uterm-go/vnc/litevirt_human.go
git commit -m "refactor(go): SetReadLimit lives in exactly one place

The server paths were already bounded and are not the defect. Routing
them through the same helper is what keeps the next unbounded call site
from being one grep miss away — which is how five of them accumulated.

Also folds the inline 1 << 20 in the VNC path into the shared constant;
it was a fourth copy of the same number."
```

---

### Task 5: Cross-language WebSocket limit scenarios

**Files:**
- Create: `spec/ws_message_limit_scenarios.json`
- Create: `tests/conformance/test_ws_limit_parity.py`

**Interfaces:**
- Consumes: the scenario-runner conventions in `tests/conformance/backends.py`.
- Produces: `spec/ws_message_limit_scenarios.json`.

Read `spec/fanout_security_scenarios.json` and
`tests/conformance/test_fanout_security_coverage.py` first and follow their
shape.

- [ ] **Step 1: Write the scenario file**

Four scenarios:

| Scenario ID | Input | Expected |
|---|---|---|
| `wslimit_001_under_limit_accepted` | message of `limit - 1` bytes | delivered intact |
| `wslimit_002_exact_limit_accepted` | message of exactly `limit` bytes | delivered intact |
| `wslimit_003_over_limit_refused` | message of `limit + 1` bytes | connection closed, one stable error, no partial delivery |
| `wslimit_004_fragmented_over_limit_refused` | over-limit message split across many fragments, each individually under the limit | closed, same error as 003 |

`wslimit_004` is the one that matters most: a per-fragment check passes it and a
per-message check does not, and that distinction is invisible to any test that
only sends single-frame messages.

- [ ] **Step 2: Write the runner and run it against Python**

Run: `uv run pytest tests/conformance/test_ws_limit_parity.py -v`

Expected: PASS. Python is the oracle
(`packages/provide-uterm-server/src/provide/uterm/server/bridge/worker_link.py:273`
passes `max_size` to the websockets library, which enforces per-message).

- [ ] **Step 3: Run it against Go, C#, and TypeScript**

Run: `uv run pytest tests/conformance/test_ws_limit_parity.py -v`

Expected: PASS on all four backends.

- [ ] **Step 4: Commit**

```bash
git add spec/ws_message_limit_scenarios.json tests/conformance/test_ws_limit_parity.py
git commit -m "test(conformance): shared WebSocket message-limit scenarios

Four scenarios: under, exactly at, over, and over-but-fragmented. The
fragmented case is the one worth having — a per-fragment check passes it
and a per-message check does not, and no single-frame test can tell the
two apart.

This is the executable evidence the semantic-safety design asks for and
the measurement found missing."
```

---

## Definition of done

Per the measurement spec, CM-01 closes when:

- `grep -rn "SetReadLimit" packages/provide-uterm-go --include="*.go" | grep -v _test`
  returns exactly one hit, in `wslimit/wslimit.go`;
- `TestApplyRefusesAnOverLimitMessage` was observed failing against
  `SetReadLimit(-1)` (Task 2, Step 3);
- `spec/ws_message_limit_scenarios.json` passes on all four backends;
- `gofmt -l .` is empty and `go vet ./...` and `go test -race ./...` pass.

Then update the CM-01 row and the Status date in
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`.

## Notes for the implementer

- Do not raise the default above 1 MiB to make something pass. If a real
  terminal frame exceeds 1 MiB, that is a finding about the frame, and the
  server has been rejecting it at that size all along — the client paths were
  simply not noticing.
- `Resolve(0)` meaning "unset" rather than "unlimited" is deliberate. Go's zero
  value for an unset `int` config field is 0, so the safe reading of an omitted
  setting must be the default and not the absence of a bound.
- `gateway/pump.go` proxies between two connections. Bounding the read on one
  side does not bound the other; check whether the peer connection in that
  function also needs `wslimit.Apply` before considering the site done.
