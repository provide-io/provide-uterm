//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"time"
)

const lifecycleOperationTimeout = 5 * time.Second

func boundedOperationContext(ctx context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(ctx, lifecycleOperationTimeout)
}

func statePendingDone(st *WorkerTermState, includeInput bool) <-chan struct{} {
	if st == nil {
		return nil
	}
	if st.LifecyclePending != nil {
		return st.LifecyclePending.Done
	}
	if includeInput && st.InputSendPending != nil {
		return st.InputSendPending.Done
	}
	return nil
}

func (h *TermHub) beginLifecycleLocked(st *WorkerTermState, kind string) *LifecycleReservation {
	reservation := &LifecycleReservation{
		Kind: kind, Worker: st.WorkerWS, WorkerGeneration: st.WorkerGeneration, Done: make(chan struct{}),
	}
	st.LifecyclePending = reservation
	return reservation
}

func (h *TermHub) finishLifecycle(workerID string, reservation *LifecycleReservation) {
	h.lock.Lock()
	if st := h.registry.Get(workerID); st != nil && st.LifecyclePending == reservation {
		st.LifecyclePending = nil
	}
	close(reservation.Done)
	h.lock.Unlock()
}

func (h *TermHub) markBrowserResumeOwnerLocked(ctx context.Context, ws BrowserConn, owner bool) error {
	if h.resumeStore == nil {
		return nil
	}
	token := h.wsToResumeToken[ws]
	if token == "" {
		return nil
	}
	return h.resumeStore.MarkHijackOwner(ctx, token, owner)
}

// WaitResumeTokenReady permits a token used by its currently bound socket and
// otherwise waits for the old socket's disconnect bookkeeping to detach it.
func (h *TermHub) WaitResumeTokenReady(ctx context.Context, token string, ws BrowserConn) error {
	h.lock.Lock()
	if h.wsToResumeToken[ws] == token {
		h.lock.Unlock()
		return nil
	}
	done := h.resumeTokenDetached[token]
	h.lock.Unlock()
	if done == nil {
		return nil
	}
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (h *TermHub) detachResumeTokenLocked(token string) {
	if done := h.resumeTokenDetached[token]; done != nil {
		delete(h.resumeTokenDetached, token)
		close(done)
	}
}
