//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "context"

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
	ok, reason := h.Lease.TryAcquireWs(workerID, ws)
	if ok {
		h.emitTelemetry(ctx, "hijack.acquired", workerID, nil, nil,
			map[string]any{"hijack_type": "dashboard", "lease_s": h.Lease.DashboardHijackLeaseS()})
	}
	return ok, reason
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
