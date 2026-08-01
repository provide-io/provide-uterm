//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"strings"
)

// This file holds the lease + polling facade delegators on TermHub. The
// telemetry-emitting wrappers mirror core_delegates_lease.py; the rest are thin
// pass-throughs so the server wave has a single facade surface. New code may
// prefer hub.Lease.<method> / hub.Polling.<method> directly.

// CleanupExpiredHijack expires stale REST/dashboard leases, emitting an
// expired-telemetry event per lease type when cleanup fires. Port of
// core_delegates_lease.cleanup_expired_hijack.
func (h *TermHub) CleanupExpiredHijack(ctx context.Context, workerID string) (bool, error) {
	h.lock.Lock()
	now := h.clock.Monotonic()
	st := h.registry.Get(workerID)
	hadRest := st != nil && st.HijackSession != nil && st.HijackSession.LeaseExpiresAt <= now
	hadDashboard := st != nil && st.HijackOwner != nil && st.HijackOwnerExpiresAt != nil && *st.HijackOwnerExpiresAt <= now
	h.lock.Unlock()

	cleaned, err := h.Lease.CleanupExpired(ctx, workerID)
	if err != nil {
		return false, err
	}
	if cleaned {
		if hadRest {
			h.emitTelemetry(ctx, "hijack.expired", workerID, nil, nil, map[string]any{"hijack_type": "rest"})
		}
		if hadDashboard {
			h.emitTelemetry(ctx, "hijack.expired", workerID, nil, nil, map[string]any{"hijack_type": "dashboard"})
		}
	}
	return cleaned, nil
}

// GetRestSession runs a telemetry-aware cleanup pass then returns the active
// REST session matching hijackID. Port of core_delegates_lease.get_rest_session.
func (h *TermHub) GetRestSession(ctx context.Context, workerID, hijackID string) (*HijackSession, error) {
	if _, err := h.CleanupExpiredHijack(ctx, workerID); err != nil {
		return nil, err
	}
	return h.Lease.getRestSessionNoCleanup(workerID, hijackID), nil
}

// TryAcquireRestHijack reserves a REST hijack and emits acquire telemetry on
// success. Port of core_delegates_lease.try_acquire_rest_hijack.
func (h *TermHub) TryAcquireRestHijack(
	ctx context.Context, workerID, owner string, leaseS int, hijackID string, now float64,
) (bool, string, error) {
	ok, reason, err := h.Lease.TryAcquireRest(ctx, workerID, owner, leaseS, hijackID, now)
	if err != nil {
		return false, reason, err
	}
	if ok {
		h.emitTelemetry(ctx, "hijack.acquired", workerID, strp(owner), nil,
			map[string]any{"hijack_type": "rest", "lease_s": leaseS})
	}
	return ok, reason, nil
}

// TryAcquireWsHijack sets the dashboard-WS hijack owner and emits acquire
// telemetry on success. Port of core_delegates_lease.try_acquire_ws_hijack.
func (h *TermHub) TryAcquireWsHijack(ctx context.Context, workerID string, ws BrowserConn) (bool, string) {
	opCtx, cancel := boundedOperationContext(ctx)
	defer cancel()
	for {
		h.lock.Lock()
		st := h.registry.Get(workerID)
		if st == nil || st.WorkerWS == nil {
			h.lock.Unlock()
			return false, "no_worker"
		}
		if st.LifecyclePending != nil && strings.HasSuffix(st.LifecyclePending.Kind, "acquire_pause") {
			h.lock.Unlock()
			return false, "already_hijacked"
		}
		if done := statePendingDone(st, true); done != nil {
			h.lock.Unlock()
			if waitInputReservation(opCtx, done) != nil {
				return false, "cancelled"
			}
			continue
		}
		if h.State.IsDashboardHijackActive(st) || h.State.HasValidRESTLease(st) || st.HijackPending != nil {
			h.lock.Unlock()
			return false, "already_hijacked"
		}
		exp := h.clock.Monotonic() + float64(h.Lease.DashboardHijackLeaseS())
		st.setDashboardOwner(ws, &exp)
		if err := h.markBrowserResumeOwnerLocked(opCtx, ws, true); err != nil {
			st.clearDashboardOwner()
			h.lock.Unlock()
			return false, "resume_store"
		}
		h.lock.Unlock()
		break
	}
	ok, reason := true, ""
	if ok {
		h.emitTelemetry(ctx, "hijack.acquired", workerID, nil, nil,
			map[string]any{"hijack_type": "dashboard", "lease_s": h.Lease.DashboardHijackLeaseS()})
	}
	return ok, reason
}

// AcquireWsHijackAndPause reserves the complete pause/acquire transition, so
// no release, reconnect, or competing acquire can interleave between the
// worker pause and publishing the dashboard owner.
func (h *TermHub) AcquireWsHijackAndPause(ctx context.Context, workerID string, ws BrowserConn) (bool, string) {
	opCtx, cancel := boundedOperationContext(ctx)
	defer cancel()
	var lifecycle *LifecycleReservation
	var worker WorkerWS
	var workerGeneration uint64
	for {
		h.lock.Lock()
		st := h.registry.Get(workerID)
		if st == nil || st.WorkerWS == nil {
			h.lock.Unlock()
			return false, "no_worker"
		}
		if done := statePendingDone(st, true); done != nil {
			h.lock.Unlock()
			if waitInputReservation(opCtx, done) != nil {
				return false, "cancelled"
			}
			continue
		}
		if h.State.IsDashboardHijackActive(st) || h.State.HasValidRESTLease(st) || st.HijackPending != nil {
			h.lock.Unlock()
			return false, "already_hijacked"
		}
		if st.IsTunnelWorker {
			h.lock.Unlock()
			return false, OwnedInputUnsupported
		}
		worker = st.WorkerWS
		workerGeneration = st.WorkerGeneration
		lifecycle = h.beginLifecycleLocked(st, "ws_acquire_pause")
		h.lock.Unlock()
		break
	}
	defer h.finishLifecycle(workerID, lifecycle)
	if err := h.Router.deliverWorker(opCtx, worker, false, pauseFrame("dashboard", "", h.clock.Wall())); err != nil {
		if opCtx.Err() != nil {
			return false, "cancelled"
		}
		h.lock.Lock()
		if st := h.registry.Get(workerID); st != nil && st.WorkerWS == worker && st.WorkerGeneration == workerGeneration {
			st.WorkerWS = nil
			st.WorkerGeneration++
		}
		h.lock.Unlock()
		return false, "no_worker"
	}
	h.lock.Lock()
	st := h.registry.Get(workerID)
	if st == nil || st.LifecyclePending != lifecycle || st.WorkerWS != worker || st.WorkerGeneration != workerGeneration {
		h.lock.Unlock()
		return false, "no_worker"
	}
	exp := h.clock.Monotonic() + float64(h.Lease.DashboardHijackLeaseS())
	st.setDashboardOwner(ws, &exp)
	if err := h.markBrowserResumeOwnerLocked(opCtx, ws, true); err != nil {
		st.clearDashboardOwner()
		h.lock.Unlock()
		_, _ = h.Router.SendWorker(opCtx, workerID, resumeFrame("dashboard", h.clock.Wall()), nil)
		return false, "resume_store"
	}
	h.lock.Unlock()
	h.emitTelemetry(opCtx, "hijack.acquired", workerID, nil, nil,
		map[string]any{"hijack_type": "dashboard", "lease_s": h.Lease.DashboardHijackLeaseS()})
	return true, ""
}

func (h *TermHub) ReleaseWsHijack(
	ctx context.Context, workerID string, ws BrowserConn,
) (released, restActive bool, err error) {
	opCtx, cancel := boundedOperationContext(ctx)
	defer cancel()
	var lifecycle *LifecycleReservation
	for {
		h.lock.Lock()
		st := h.registry.Get(workerID)
		if st == nil || !h.State.IsDashboardHijackActive(st) || st.HijackOwner != ws {
			restActive = st != nil && h.State.HasValidRESTLease(st)
			h.lock.Unlock()
			return false, restActive, nil
		}
		if done := statePendingDone(st, true); done != nil {
			h.lock.Unlock()
			if err := waitInputReservation(opCtx, done); err != nil {
				return false, false, err
			}
			continue
		}
		if err := h.markBrowserResumeOwnerLocked(opCtx, ws, false); err != nil {
			h.lock.Unlock()
			return false, false, err
		}
		st.clearDashboardOwner()
		restActive = h.State.HasValidRESTLease(st)
		if !restActive {
			lifecycle = h.beginLifecycleLocked(st, "ws_release_resume")
		}
		h.lock.Unlock()
		break
	}
	if lifecycle != nil {
		defer h.finishLifecycle(workerID, lifecycle)
		if sent, sendErr := h.SendWorker(opCtx, workerID, resumeFrame("dashboard", h.clock.Wall())); sendErr != nil {
			return true, restActive, sendErr
		} else if !sent {
			return true, restActive, context.Canceled
		}
	}
	h.emitTelemetry(opCtx, "hijack.released", workerID, nil, nil, map[string]any{"hijack_type": "dashboard"})
	return true, restActive, nil
}

func (h *TermHub) ReleaseRestHijackAndResume(
	ctx context.Context, workerID, hijackID string,
) (released, shouldResume bool, err error) {
	opCtx, cancel := boundedOperationContext(ctx)
	defer cancel()
	owner := "rest"
	var lifecycle *LifecycleReservation
	for {
		h.lock.Lock()
		st := h.registry.Get(workerID)
		if st == nil || st.HijackSession == nil || st.HijackSession.HijackID != hijackID {
			h.lock.Unlock()
			return false, false, nil
		}
		if done := statePendingDone(st, true); done != nil {
			h.lock.Unlock()
			if err := waitInputReservation(opCtx, done); err != nil {
				return false, false, err
			}
			continue
		}
		owner = st.HijackSession.Owner
		st.HijackSession = nil
		shouldResume = !h.State.IsDashboardHijackActive(st)
		if shouldResume {
			lifecycle = h.beginLifecycleLocked(st, "rest_release_resume")
		}
		h.lock.Unlock()
		break
	}
	if lifecycle != nil {
		defer h.finishLifecycle(workerID, lifecycle)
		if sent, sendErr := h.SendWorker(opCtx, workerID, resumeFrame(owner, h.clock.Wall())); sendErr != nil {
			return true, shouldResume, sendErr
		} else if !sent {
			return true, shouldResume, context.Canceled
		}
	}
	return true, shouldResume, nil
}

// TryReleaseWsHijack verifies ownership and clears the dashboard hijack,
// emitting release telemetry on success. Port of try_release_ws_hijack.
func (h *TermHub) TryReleaseWsHijack(ctx context.Context, workerID string, ws BrowserConn) (bool, bool) {
	released, restActive := h.Lease.TryReleaseWs(workerID, ws)
	if released {
		h.emitTelemetry(ctx, "hijack.released", workerID, nil, nil, map[string]any{"hijack_type": "dashboard"})
	}
	return released, restActive
}

// TouchHijackOwner extends the dashboard-WS hijack lease.
func (h *TermHub) TouchHijackOwner(workerID string, leaseS *int) *float64 {
	return h.Lease.TouchOwner(workerID, leaseS)
}

// TouchIfOwner extends the dashboard lease iff ws still owns it.
func (h *TermHub) TouchIfOwner(workerID string, ws BrowserConn) *float64 {
	return h.Lease.TouchIfOwner(workerID, ws)
}

// ExtendHijackLease extends the REST hijack lease on a heartbeat.
func (h *TermHub) ExtendHijackLease(workerID, hijackID, owner string, leaseS int, now float64) *float64 {
	return h.Lease.ExtendLease(workerID, hijackID, owner, leaseS, now)
}

// GetFreshHijackExpiry re-reads the current REST lease expiry, or fallback.
func (h *TermHub) GetFreshHijackExpiry(workerID, hijackID string, fallback float64) float64 {
	return h.Lease.GetFreshExpiry(workerID, hijackID, fallback)
}

// GetHijackEventsData returns the events payload for a REST hijack events endpoint.
func (h *TermHub) GetHijackEventsData(
	workerID, hijackID string, hs *HijackSession, afterSeq, limit int,
) map[string]any {
	return h.Lease.GetEventsData(workerID, hijackID, hs, afterSeq, limit)
}

// CheckHijackValid reports whether the REST hijack session is still valid.
func (h *TermHub) CheckHijackValid(workerID, hijackID string) bool {
	return h.Lease.CheckValid(workerID, hijackID)
}

// ReleaseRestHijack clears the REST hijack session, returning (released, shouldResume).
func (h *TermHub) ReleaseRestHijack(workerID, hijackID string) (bool, bool) {
	return h.Lease.ReleaseRest(workerID, hijackID)
}

// CheckStillHijacked reports whether any hijack (REST or dashboard) is active.
func (h *TermHub) CheckStillHijacked(workerID string) bool { return h.Lease.StillHijacked(workerID) }

// IsInputOpenMode reports whether the worker is in open input mode.
func (h *TermHub) IsInputOpenMode(workerID string) bool { return h.Lease.IsInputOpenMode(workerID) }

// PrepareBrowserInput reports whether ws may send input (extending the lease if owner).
func (h *TermHub) PrepareBrowserInput(workerID string, ws BrowserConn) bool {
	return h.Lease.PrepareBrowserInput(workerID, ws)
}

// -- Polling facade ----------------------------------------------------------

// WaitForSnapshot requests a fresh snapshot and polls until one arrives.
func (h *TermHub) WaitForSnapshot(ctx context.Context, workerID string, timeoutMs int) (map[string]any, error) {
	return h.Polling.WaitForSnapshot(ctx, workerID, timeoutMs)
}

// WaitForGuard polls until the snapshot satisfies the prompt-id/regex guards.
func (h *TermHub) WaitForGuard(
	ctx context.Context, workerID, expectPromptID, expectRegex string, timeoutMs, pollIntervalMs int,
) (bool, map[string]any, string, error) {
	return h.Polling.WaitForGuard(ctx, workerID, expectPromptID, expectRegex, timeoutMs, pollIntervalMs)
}
