//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"

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
	// Phase 1 — reserve under the lock (in-memory only).
	lm.lock.Lock()
	st := lm.registry.Get(workerID)
	if st == nil || st.WorkerWS == nil {
		lm.lock.Unlock()
		return false, "no_worker", nil
	}
	if st.InputMode == InputModeOpen {
		lm.lock.Unlock()
		return false, "open_mode", nil
	}
	if lm.hub.IsDashboardHijackActive(st) || lm.hub.HasValidRESTLease(st) || st.HijackPending != nil {
		lm.lock.Unlock()
		return false, "already_hijacked", nil
	}
	workerWS := st.WorkerWS
	st.HijackPending = strp(hijackID)
	lm.lock.Unlock()

	// finally — roll back a still-outstanding reservation (no-op on success).
	defer func() {
		lm.lock.Lock()
		st := lm.registry.Get(workerID)
		if st != nil && st.HijackPending != nil && *st.HijackPending == hijackID {
			st.HijackPending = nil
		}
		lm.lock.Unlock()
	}()

	// Phase 2 — pause the worker OUTSIDE the lock.
	encoded, encErr := controlchannel.EncodeControlFrame(pauseFrame(owner, hijackID, lm.clock.Wall()))
	if encErr != nil {
		return false, "", encErr
	}
	if sendErr := workerWS.SendText(ctx, encoded); sendErr != nil {
		if ctx.Err() != nil {
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
	if st == nil || st.HijackPending == nil || *st.HijackPending != hijackID {
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
	lm.lock.Lock()
	st := lm.registry.Get(workerID)
	if st == nil || st.WorkerWS == nil {
		lm.lock.Unlock()
		return false, "no_worker"
	}
	// HijackPending: REST two-phase reserve — treat as already taken so the
	// dashboard WS cannot dual-own during the pause I/O window.
	if lm.hub.IsDashboardHijackActive(st) || lm.hub.HasValidRESTLease(st) || st.HijackPending != nil {
		lm.lock.Unlock()
		return false, "already_hijacked"
	}
	ttl := lm.dashboardLeaseS
	st.HijackOwner = ws
	exp := lm.clock.Monotonic() + float64(ttl)
	st.HijackOwnerExpiresAt = &exp
	lm.lock.Unlock()
	lm.logger.Info(eventHijackAcquired, "worker_id", workerID, "hijack_type", "dashboard", "lease_s", ttl)
	return true, ""
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
	lm.lock.Lock()
	defer lm.lock.Unlock()
	st := lm.registry.Get(workerID)
	if st == nil || st.HijackSession == nil || st.HijackSession.HijackID != hijackID {
		return false, false
	}
	st.HijackSession = nil
	shouldResume = !lm.hub.IsDashboardHijackActive(st)
	return true, shouldResume
}
