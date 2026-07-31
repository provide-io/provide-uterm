# C# Pause-to-Replacement Handoff Design

## Goal

Keep input fenced continuously when an acquisition pause sequence hands control to an already-reserved worker replacement.

## Handoff invariant

An incomplete acquisition pause uses `HijackPending` for its pause reservation while `PendingPauseCompletion` fences a replacement. The replacement already owns `ActiveLifecycleTransition`, but its reservation is temporarily hidden so the acquisition can validate and commit its exact pause reservation.

When the final pause reservation and pause obligation clear, `CompletePauseSequenceIfIdle` performs the handoff under `SharedLock`. Before it detaches or signals `PendingPauseCompletion`, it copies only `ActiveLifecycleTransition.Reservation` into `HijackPending`. Input admission therefore sees a non-null lifecycle intent and waits on the active transition's existing `DisconnectResumeCompletion` throughout the interval between pause completion and the replacement continuation reacquiring the lock.

Queued replacements are not mirrored. They remain ordered behind the active node by `LifecycleTransitionCoordinator`, and normal active-node completion promotes and mirrors each successor. If there is no active replacement, `HijackPending` remains clear and input proceeds normally after the pause sequence.

## Deterministic test seam

`ConnectionManager` gains a null-default internal asynchronous callback invoked only after a replacement's `PendingPauseCompletion` await succeeds and before its registration loop reacquires `SharedLock`. Production behavior is unchanged when the callback is null.

The regression test delays a successful dashboard pause, reserves replacement R1, installs the callback, and releases the pause. The callback freezes R1 in the exact handoff window. Open-mode admin input started during that window must remain pending and must not reach the predecessor. Releasing the callback lets R1 install; the same input then completes against R1.

## Completion-path audit

Successful and repaired REST/dashboard acquisition paths converge through their `finally` blocks into `CompletePauseSequenceIfIdle` and receive the new handoff. Identity-gated worker deregistration is already safe: it clears pause state, preserves replacement nodes through `LifecycleTransitionCoordinator.Clear`, activates and mirrors the first preserved replacement, and only then signals the captured pause completion while still holding the lock. Force-release and browser-cleanup cancellation paths are completed by the acquisition `finally` path and therefore share the centralized handoff.

## Verification

- Prove the new deterministic handoff test fails before the production change and passes afterward.
- Re-run the pause FIFO, replacement cancellation, acquisition repair, and full lifecycle integration tests.
- Run the complete C# suite and `git diff --check` before committing.
