# C# Pause-Fence FIFO and Resume Reconciliation Design

## Goal

Preserve replacement arrival order while a successful acquisition pause or compensating resume is still pending, and make every failed release/expiry/force resume reconcile the captured worker through the same identity-gated disconnect path.

## Replacement ordering across the pause fence

`PendingPauseCompletion` remains the fence that prevents a replacement from displacing a worker until acquisition pause/repair has finished. A replacement must no longer wait on that fence without lifecycle ownership. Before waiting, the first replacement reserves the worker's active lifecycle node using its exact replacement reservation and completion source. A second replacement observes that active node and enqueues its own successor node through `LifecycleTransitionCoordinator`; further replacements join the same FIFO.

The active replacement waits for the pause completion while retaining its node. When the pause/repair sequence finishes, only that replacement may install its worker. Completing its exact node activates the next replacement, so R1 installs before R2. Input arriving after both replacements sees the lifecycle transition and waits until the replacement queue drains; it is therefore delivered only to final R2. Existing cancellation cleanup locates the caller's exact reservation/completion pair and completes only that node, preserving successor progress without weakening arrival order.

No separate pause-replacement queue is introduced. The existing lifecycle coordinator remains the sole ordering authority for replacement and input interaction.

## Failed resume reconciliation

`CompleteResumeAsync` uses the same bounded-send discipline as acquisition repair and reserved input delivery. It captures the worker send task, links caller cancellation with `ResumeSendTimeout`, and applies `WaitAsync` so a transport that ignores cancellation cannot strand the lifecycle transition. A timed-out or faulted task is observed asynchronously to prevent a later fault from becoming unobserved.

Every unsuccessful resume, including a worker already inactive before send, routes through `ReconcileFailedWorkerSendAsync`. That helper aborts abortable transports when possible and then calls `ReconcileWorkerDisconnectAsync` with the exact captured worker identity. The centralized reconciliation clears only that still-current worker, marks its server registry entry offline, publishes one `worker_disconnected` frame and one current-state update, and clears/advances the lifecycle transition queue. A later WebSocket receive-loop finalizer or late send completion fails the identity check and cannot remove or mark a replacement offline.

`CompleteResumeAsync` still completes its own transition in `finally`, but the lookup is idempotent: if centralized reconciliation already cleared and advanced it, there is no matching node to complete again. Ownership was already cleared by release, expiry, or force before resume begins, so disconnect reconciliation does not generate a second ownership-release callback.

## Tests

- Hold a successful acquisition pause/repair fence, start replacements R1 and R2 in known arrival order, then start later input. Assert the active/queued lifecycle nodes encode R1 then R2, release the fence, and verify replacement completion order is R1 then R2, final worker identity is R2, and R1 receives no input.
- Cover REST release, expiry, and force with both local and WebSocket-like workers, for both hanging and throwing resume sends (twelve rows total).
- In every resume-failure row, assert the captured worker is fenced and offline, disconnect/current-state publication occurs exactly once, a queued replacement progresses, and replaying a late finalizer cannot overwrite the replacement's online state or add another publication.
- Run the focused lifecycle tests and the complete C# suite.
