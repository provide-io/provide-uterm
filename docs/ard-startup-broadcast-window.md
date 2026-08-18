# ARD: The Broadcast Window a Connecting Browser Cannot See

## Status

Proposed — the defect is measured, the fix is not written.

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

## Recommendation

Option 1, in its own change: the reference first, then Go and C#, with a
conformance scenario that connects a browser and sends traffic inside the
window. It is a behavioural change to a hot path in three ports, so it does
not belong folded into an unrelated PR.

Until then the flake stands, and `multi-backend-playwright (csharp)` should be
read as this defect rather than as an infrastructure wobble.

## Consequences

- A dropped inspect row is invisible: nothing logs it, nothing retries it, and
  the UI cannot tell a session with no requests from one whose requests it
  missed.
- The terminal path is unaffected either way, because `initial_snapshot`
  already covers it. Any fix should keep that true rather than start
  double-delivering screen state.
