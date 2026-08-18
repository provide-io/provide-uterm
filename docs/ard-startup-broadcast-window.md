# ARD: The Broadcast Window a Connecting Browser Cannot See

## Status

Accepted — implemented in all three ports.

## Problem

A browser registers, receives its startup frames, and only then starts
receiving broadcasts. All three ports do this deliberately:

| port | mechanism |
|---|---|
| Python | `register_browser(..., defer_broadcast=True)` adds the socket to `hub._startup_pending_browsers`; `activate_browser_broadcasts` discards it |
| Go | `RegisterBrowser(bg, workerID, bc, role, true)` |
| C# | `RegisterBrowser(..., deferBroadcast: true)` then `ActivateBrowserBroadcasts(workerId, conn)` |

The deferral exists for ordering: `hello`, `hijack_state` and `presence_sync`
must reach the browser before anything else does.

The frames broadcast during that window are **dropped, not queued**. The
reference filters the socket out of the recipient list and moves on:

```python
# bridge/hub/router_broadcast.py:196
(ws, role) for ws, role in st.browsers.items() if ws not in hub._startup_pending_browsers
```

Nothing replays them. For terminal output that is harmless and probably why it
was written this way — the `hello` carries `initial_snapshot`, so a missed
`term` frame is superseded by the screen the browser is handed on arrival.

The HTTP/inspect channel rides the same broadcast path and has no equivalent.
There is no snapshot of past requests, and the store appends unconditionally:

```ts
// stores/inspectStore.ts:68
addRequest: (req) => set((s) => ({ exchanges: [...s.exchanges, { id: req.id, ... }] })),
```

No dedupe by id, no reconciliation against server state. A request that is
broadcast while the browser is still in its startup window is not late — it is
gone, and the inspect list is silently missing a row for the rest of the
session.

## Evidence

`multi-backend-playwright` is the only place this has surfaced, and it
surfaced as flakiness rather than as a bug:

| run | python | go | csharp |
|---|---|---|---|
| `8cb769af` | pass | pass | **fail** |
| `ac96e3e6` | pass | pass | pass |
| `aa246c74` | pass | pass | **fail** |

Two different tests failed on the two red runs, both in
`test_inspect_e2e.py`, and the second names the mechanism exactly:

```
AssertionError: Locator expected to be visible
Error: element(s) not found
```

The row never arrives. Not late, not covered, not unclickable — absent.

Both tests send their request immediately after asserting the UI shows
"Connected". That string is a client-side state: the browser's socket is open.
It says nothing about whether the server has finished the startup sequence and
activated broadcasts for that socket. The tests are asserting a guarantee the
system does not make.

Only C# fails, but C# is not where the defect is. All three ports drop; C#'s
pre-activation sequence is the longest (three awaited sends plus the DeckMux
connect), so its window is the widest and it is the one that loses the race on
a contended runner. Python and Go have the same hole and have not yet fallen
in it.

Not reproducible locally against a real C# server (5 passed, 18s against CI's
22s), which is what a window that only opens under contention looks like.

## Options

1. **Buffer and flush.** Queue frames per socket during the window, send them
   in order on activation. Preserves the ordering guarantee the deferral
   exists for, loses nothing, and fixes all three ports at the source. Costs a
   per-socket queue on a hot path, needs a bound so a browser that never
   activates cannot grow it without limit, and lands in three ports plus the
   conformance corpus.
2. **Narrow the window.** Activate broadcasts earlier in the sequence. Cheaper,
   but it trades one ordering guarantee for another and leaves the hole open —
   a smaller window is still a window.
3. **Make the test tolerate it.** Rejected. The obvious form — re-send until
   the row appears — produces duplicate rows, because `addRequest` does not
   dedupe, and the duplicate then trips Playwright's strict mode on the very
   locators the test asserts with. There is also no observable signal for
   "broadcasts are now active" to wait on, which is the same missing guarantee
   from the other side.

## Decision

Option 1, reference first, then Go and C#.

Frames the startup sequence does not already carry — decided by
`_survives_startup_window` / `survivesStartupWindow` / `SurvivesStartupWindow`,
which today means the http channel — are held per socket and delivered on
activation in arrival order. Terminal output is still dropped, deliberately:
`initial_snapshot` covers it and replaying would print the screen twice.

Three properties the implementations share, each chosen against an
alternative that looked simpler:

- **The socket stays pending until its queue drains.** Releasing it first and
  then flushing would let a frame broadcast mid-flush overtake the ones
  already waiting, which reorders the very list this exists to keep intact.
- **The queue refuses at its cap rather than evicting.** Dropping the newest
  loses the tail of a session; dropping the oldest loses its beginning *and*
  renumbers everything the user already saw.
- **A socket whose flush fails is left pending.** Pending means the broadcast
  path skips it, which is the right resting state for a connection that just
  failed a write; the disconnect handler clears both. Releasing it would send
  every subsequent broadcast into a dead socket.

The defensive guard in activate — a browser that disconnected mid-startup is
left pending on purpose — is preserved verbatim in all three.

C# also had to change shape: `ActivateBrowserBroadcasts` became
`ActivateBrowserBroadcastsAsync`, because delivering a backlog is I/O and
doing it under `SharedLock` would hold the hub's lock across a socket write.

Each port carries a mirrored regression suite (seven cases) that goes red on
its own pre-fix code — Python 4 of 7, Go and C# 3 of 7, the difference being
that the backlog-cleanup cases pass trivially when there is no backlog to
clean. A live conformance scenario driving a real browser into the window was
considered and not written: the per-port suites pin the same behaviour at the
hub boundary, where the defect actually lives, without a socket-level harness
whose own timing would be the thing under test.

## Consequences

- A dropped inspect row is invisible: nothing logs it, nothing retries it, and
  the UI cannot tell a session with no requests from one whose requests it
  missed.
- The terminal path is unaffected, because `initial_snapshot` already covers
  it and the fix deliberately keeps dropping `term` rather than start
  double-delivering screen state.
- The rule is one predicate per port rather than a frame-type allowlist, so a
  future channel that is also append-only — anything the browser accumulates
  rather than replaces — is one line to include, and the reasoning for why it
  belongs is written down next to it.
- `multi-backend-playwright (csharp)` should stop flaking. If it does not, the
  remaining cause is not this: the row now survives the window by
  construction, and the per-port suites would have to be wrong together.
