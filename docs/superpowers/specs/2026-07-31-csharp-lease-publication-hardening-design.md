# C# Lease Publication Hardening Design

## Goal

Make worker pause/repair sends unable to strand lease lifecycle state, and make every external ownership callback describe the ownership generation that is still authoritative when the callback is emitted.

## Worker-send lifecycle

REST and dashboard acquisition pause sends, plus compensating resumes after a pause may have landed, use one bounded lease-send primitive. The primitive passes a linked timeout token, independently enforces `ResumeSendTimeout` with `WaitAsync`, and observes eventual faults from cancellation-ignoring transports.

A failed or timed-out pause is uncertain delivery. The lease manager first attempts the required bounded compensating resume, then aborts and reconciles the captured worker. A failed compensating resume also aborts and reconciles that captured worker. Reconciliation remains identity-gated, so a replacement is never fenced, while lifecycle clear releases queued replacements.

## Ownership publication

Ownership mutations capture a typed publication token containing the worker ID, `HijackOwnershipVersion`, expected state (held or released), and expected owner identity. The StateStore per-worker sequencer revalidates that token against current lease state before invoking the host callback. Ordinary true and false notifications use the same sequencer.

REST acquire, REST release, forced release, expiry, and worker disconnect publish using the token captured with their mutation. Disconnect publishes false only when the captured worker actually held ownership. Browser `hijack_state` remains a current-state snapshot emitted after lifecycle work, so a successor's state wins.

## Tests

- REST/dashboard pause hang and throw, each with a queued replacement.
- REST/dashboard compensating-resume hang and throw, each with a queued replacement.
- Delayed acquire, release, and forced-release publication while a successor ownership mutation wins.
- A never-hijacked disconnect emits no ownership-loss callback.
- Existing stale disconnect and browser ordering coverage remains green.

All focused lifecycle tests and the complete C# suite must pass.
