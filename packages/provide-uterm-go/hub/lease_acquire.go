//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

// TryAcquireRest reserves a REST hijack, pauses the worker, then finalises the
// lease. Port of HijackLeaseManager.try_acquire_rest.
//
// The worker-pause send runs OUTSIDE the hub lock (holding it across a
// backpressured worker send would stall every other hub operation). The slot
// is reserved under the lock (HijackPending), the pause is sent lock-free, and
// the lease is finalised under the lock; a deferred rollback clears a stuck
// reservation on failure/cancellation.
//
// Returns (ok, reason, err). reason is "" on success (the Python None); err is
// non-nil only for an encode failure or a cancelled context.
func (lm *HijackLeaseManager) TryAcquireRest(
	ctx context.Context,
	workerID string,
	owner string,
	leaseS int,
	hijackID string,
	now float64,
) (ok bool, reason string, err error) {
	opCtx, cancel := boundedOperationContext(ctx)
	defer cancel()
	// Phase 1 — reserve under the lock (in-memory only).
	var st *WorkerTermState
	var workerWS WorkerWS
	var workerGeneration uint64
	var lifecycle *LifecycleReservation
	for {
		lm.lock.Lock()
		st = lm.registry.Get(workerID)
		if st == nil || st.WorkerWS == nil {
			lm.lock.Unlock()
			return false, "no_worker", nil
		}
		if st.LifecyclePending != nil && strings.HasSuffix(st.LifecyclePending.Kind, "acquire_pause") {
			lm.lock.Unlock()
			return false, "already_hijacked", nil
		}
		if done := statePendingDone(st, true); done != nil {
			lm.lock.Unlock()
			if err := waitInputReservation(opCtx, done); err != nil {
				return false, "", err
			}
			continue
		}
		if st.InputMode == InputModeOpen {
			lm.lock.Unlock()
			return false, "open_mode", nil
		}
		if lm.hub.IsDashboardHijackActive(st) || lm.hub.HasValidRESTLease(st) || st.HijackPending != nil {
			lm.lock.Unlock()
			return false, "already_hijacked", nil
		}
		workerWS = st.WorkerWS
		workerGeneration = st.WorkerGeneration
		st.HijackPending = strp(hijackID)
		lifecycle = &LifecycleReservation{
			Kind: "rest_acquire_pause", Worker: st.WorkerWS,
			WorkerGeneration: st.WorkerGeneration, Done: make(chan struct{}),
		}
		st.LifecyclePending = lifecycle
		lm.lock.Unlock()
		break
	}

	// finally — roll back a still-outstanding reservation (no-op on success).
	defer func() {
		lm.lock.Lock()
		st := lm.registry.Get(workerID)
		if st != nil && st.HijackPending != nil && *st.HijackPending == hijackID {
			st.HijackPending = nil
		}
		if st != nil && st.LifecyclePending == lifecycle {
			st.LifecyclePending = nil
		}
		if lifecycle != nil {
			close(lifecycle.Done)
			lifecycle = nil
		}
		lm.lock.Unlock()
	}()

	// Phase 2 — pause the worker OUTSIDE the lock.
	encoded, encErr := controlchannel.EncodeControlFrame(pauseFrame(owner, hijackID, lm.clock.Wall()))
	if encErr != nil {
		return false, "", encErr
	}
	if sendErr := workerWS.SendText(opCtx, encoded); sendErr != nil {
		if opCtx.Err() != nil {
			// Cancellation: propagate like Python's CancelledError. The
			// deferred rollback still clears the reservation; the socket is
			// NOT nulled.
			return false, "", sendErr
		}
		lm.logger.Debug("pause_worker_failed", "worker_id", workerID, "error", sendErr)
		lm.lock.Lock()
		st := lm.registry.Get(workerID)
		// Only null the SAME socket (a mid-send reconnect must be preserved).
		if st != nil && st.WorkerWS == workerWS {
			st.WorkerWS = nil
		}
		lm.lock.Unlock()
		return false, "no_worker", nil
	}

	// Phase 3 — finalise under the lock (unless cancelled / superseded).
	lm.lock.Lock()
	st = lm.registry.Get(workerID)
	if st == nil || st.HijackPending == nil || *st.HijackPending != hijackID ||
		st.LifecyclePending != lifecycle || st.WorkerWS != workerWS || st.WorkerGeneration != workerGeneration {
		lm.lock.Unlock()
		return false, "no_worker", nil
	}
	st.HijackSession = &HijackSession{
		HijackID:       hijackID,
		Owner:          owner,
		AcquiredAt:     now,
		LeaseExpiresAt: now + float64(leaseS),
		LastHeartbeat:  now,
	}
	st.HijackPending = nil
	lm.lock.Unlock()

	lm.logger.Info(eventHijackAcquired,
		"worker_id", workerID, "hijack_type", "rest", "owner", owner, "lease_s", leaseS)
	return true, "", nil
}

// TryAcquireWs atomically checks availability and sets the dashboard-WS hijack
// owner. Port of try_acquire_ws. Returns (ok, reason).
func (lm *HijackLeaseManager) TryAcquireWs(workerID string, ws BrowserConn) (bool, string) {
	return lm.TryAcquireWsContext(context.Background(), workerID, ws)
}

func (lm *HijackLeaseManager) TryAcquireWsContext(ctx context.Context, workerID string, ws BrowserConn) (bool, string) {
	for {
		lm.lock.Lock()
		st := lm.registry.Get(workerID)
		if st == nil || st.WorkerWS == nil {
			lm.lock.Unlock()
			return false, "no_worker"
		}
		if done := statePendingDone(st, true); done != nil {
			lm.lock.Unlock()
			if waitInputReservation(ctx, done) != nil {
				return false, "cancelled"
			}
			continue
		}
		// HijackPending: REST two-phase reserve — treat as already taken so the
		// dashboard WS cannot dual-own during the pause I/O window.
		if lm.hub.IsDashboardHijackActive(st) || lm.hub.HasValidRESTLease(st) || st.HijackPending != nil {
			lm.lock.Unlock()
			return false, "already_hijacked"
		}
		ttl := lm.dashboardLeaseS
		exp := lm.clock.Monotonic() + float64(ttl)
		st.setDashboardOwner(ws, &exp)
		lm.lock.Unlock()
		lm.logger.Info(eventHijackAcquired, "worker_id", workerID, "hijack_type", "dashboard", "lease_s", ttl)
		return true, ""
	}
}

// ExtendLease extends the REST hijack lease on a heartbeat, returning the new
// expiry or nil. Port of extend_lease. An owner mismatch records the
// hijack_heartbeat_denied_owner_mismatch metric and returns nil.
func (lm *HijackLeaseManager) ExtendLease(workerID, hijackID, owner string, leaseS int, now float64) *float64 {
	lm.lock.Lock()
	defer lm.lock.Unlock()
	st := lm.registry.Get(workerID)
	if st == nil || st.HijackSession == nil || st.HijackSession.HijackID != hijackID {
		return nil
	}
	if st.HijackSession.Owner != owner {
		lm.logger.Warn("hijack_heartbeat_denied_owner_mismatch",
			"worker_id", workerID, "hijack_id", hijackID,
			"current", st.HijackSession.Owner, "attempted", owner)
		lm.hub.Metric("hijack_heartbeat_denied_owner_mismatch", 1)
		return nil
	}
	st.HijackSession.LastHeartbeat = now
	st.HijackSession.LeaseExpiresAt = now + float64(leaseS)
	exp := st.HijackSession.LeaseExpiresAt
	return &exp
}

// ReleaseRest atomically clears the REST hijack session, returning
// (released, shouldResume). shouldResume is true when no dashboard hijack
// remains. Port of release_rest.
func (lm *HijackLeaseManager) ReleaseRest(workerID, hijackID string) (released, shouldResume bool) {
	for {
		lm.lock.Lock()
		st := lm.registry.Get(workerID)
		if st == nil || st.HijackSession == nil || st.HijackSession.HijackID != hijackID {
			lm.lock.Unlock()
			return false, false
		}
		if pending := st.InputSendPending; pending != nil {
			done := pending.Done
			lm.lock.Unlock()
			<-done
			continue
		}
		st.HijackSession = nil
		shouldResume = !lm.hub.IsDashboardHijackActive(st)
		lm.lock.Unlock()
		return true, shouldResume
	}
}
