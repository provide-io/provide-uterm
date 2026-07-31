# C# Dashboard Publication Completion Design

## Goal

Route dashboard acquire, release, and ownership restoration callbacks through the same typed generation/state/identity sequencer as REST and teardown publications, without breaking the existing public tuple and boolean APIs.

## API and publication flow

Each dashboard ownership mutation gains a typed-result core. The core captures an `OwnershipPublicationToken` under the shared state lock at the same instant as the ownership mutation. Dashboard acquisition and restoration capture `DashboardHeld`, whose identity is the exact browser WebSocket object. Dashboard release captures `Released` after incrementing `HijackOwnershipVersion`.

Existing public tuple and boolean methods remain compatibility wrappers. A wrapper awaits or completes its core, lets all existing pause/resume lifecycle cleanup finish, and then submits the captured token to `StateStore.NotifyHijackChanged` exactly once. Callback payloads are `enabled=true, owner="dashboard"` for dashboard ownership and `enabled=false, owner=null` for dashboard release. The WebSocket object is validation identity only and is never exposed to the callback.

Browser disconnect cleanup is also a dashboard ownership mutation. Its typed core captures `Released` at the exact owner clear without incrementing the restorable ownership version, and its compatibility wrapper publishes before returning that version to the server's resume-token flow. If an input or lifecycle transition defers the clear, `ResumeWorkerIfOwnershipUnchangedAsync` captures and publishes the same generation-fenced release at the later exact clear before completing the disconnect-resume fence. This orders normal resume callbacks as dashboard-held, released, dashboard-held while preserving restoration of the same logical generation.

The production WebSocket handler continues to call the compatibility wrappers. Its existing `BroadcastHijackStateAsync` calls remain unchanged, so browser peers still receive current-state snapshots after dashboard request, release, and resume operations.

## Stale ordering

All dashboard tokens use the existing per-worker publication gate and current-state validation. A delayed dashboard acquisition or restoration token is rejected after release or successor ownership. A delayed disconnect-release token is rejected after restoration or successor ownership. Each valid transition produces one callback, and rejected or failed mutations produce none.

## Tests

- A production dashboard acquire followed by release emits exactly `(true, "dashboard"), (false, null)` and preserves browser state broadcasts.
- Ownership-version resume emits exactly `(true, "dashboard"), (false, null), (true, "dashboard")` and preserves its browser state broadcast.
- Immediate and deferred disconnect cleanup publish release only when the exact dashboard owner is cleared.
- Delayed dashboard-held and disconnect-release tokens are rejected after successor mutations and cannot append stale callbacks.
- Existing lifecycle-focused tests and the complete C# suite pass.
