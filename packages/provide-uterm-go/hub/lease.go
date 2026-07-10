//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"log/slog"
	"sync"
)

// Structured event names emitted by the lease manager, matching the
// provide.telemetry event() strings used by the Python original.
const (
	eventHijackAcquired = "terminal.hijack.acquired"
	eventHijackReleased = "terminal.hijack.released"
	eventHijackExpired  = "terminal.hijack.expired"
)

// LeaseHub is the subset of the composing hub the lease manager calls back
// into. Wave B's TermHub implements it; unit tests supply a fake. It mirrors
// the Python _LeaseHubCallbacks structural protocol.
type LeaseHub interface {
	// State predicates (pure reads of a worker state).
	IsHijacked(st *WorkerTermState) bool
	IsDashboardHijackActive(st *WorkerTermState) bool
	HasValidRESTLease(st *WorkerTermState) bool
	CanSendInput(st *WorkerTermState, ws BrowserConn) bool

	// Observability / callbacks.
	Metric(name string, value int)
	NotifyHijackChanged(workerID string, enabled bool, owner *string)

	// Cross-cutting side effects (the hub is the source of truth).
	SendWorker(ctx context.Context, workerID string, msg map[string]any) (bool, error)
	BroadcastHijackState(ctx context.Context, workerID string) error
	AppendEvent(ctx context.Context, workerID, eventType string) error
	PruneIfIdle(ctx context.Context, workerID string) error
	// RecheckAndResume forwards to [HijackLeaseManager.RecheckAndResume]. The
	// indirection lets wave B intercept the call (matching the Python hub
	// shim that forwards back to the manager).
	RecheckAndResume(ctx context.Context, workerID string, now float64) error
}

// HijackLeaseManager is the multi-worker hijack lease state machine. Port of
// provide.uterm.server.bridge.hub.lease.HijackLeaseManager.
//
// It owns per-worker hijack arbitration for both REST and dashboard-WS paths,
// lease-expiry sweeps, and the resume control frames emitted when both slots
// go idle. Cross-cutting side effects are dispatched through the injected
// [LeaseHub] so the manager holds no hub import.
//
// Deviation: the Python manager opens OpenTelemetry spans around acquire /
// release / heartbeat; this port omits tracing (no OTel dependency).
type HijackLeaseManager struct {
	registry        *WorkerRegistry
	lock            *sync.Mutex
	dashboardLeaseS int
	hub             LeaseHub
	clock           Clock
	logger          *slog.Logger
}

// NewHijackLeaseManager builds a manager. lock is the composing hub's shared
// mutex (reused as-is to keep lock ordering identical); a fresh mutex is fine
// for standalone use. dashboardLeaseS is clamped into [1, 600]. clock/logger
// nil select the real clock / slog.Default().
func NewHijackLeaseManager(
	registry *WorkerRegistry,
	lock *sync.Mutex,
	dashboardLeaseS int,
	hub LeaseHub,
	clock Clock,
	logger *slog.Logger,
) *HijackLeaseManager {
	return &HijackLeaseManager{
		registry:        registry,
		lock:            lock,
		dashboardLeaseS: clampDashboardLease(dashboardLeaseS),
		hub:             hub,
		clock:           orDefaultClock(clock),
		logger:          loggerOrDefault(logger),
	}
}

// clampDashboardLease clamps a dashboard-WS lease TTL into [1, 600] seconds.
func clampDashboardLease(v int) int {
	if v < 1 {
		return 1
	}
	if v > 600 {
		return 600
	}
	return v
}

// DashboardHijackLeaseS returns the configured dashboard-WS lease TTL (seconds).
func (lm *HijackLeaseManager) DashboardHijackLeaseS() int { return lm.dashboardLeaseS }

// SetDashboardHijackLeaseS updates and re-clamps the dashboard-WS lease TTL.
func (lm *HijackLeaseManager) SetDashboardHijackLeaseS(v int) {
	lm.dashboardLeaseS = clampDashboardLease(v)
}

// ComputeLeaseExpirations returns (browserExpired, restExpired) without
// mutating state. Port of the static compute_lease_expirations helper.
func ComputeLeaseExpirations(st *WorkerTermState, now float64) (browserExpired, restExpired bool) {
	l := st.Lease()
	restExpired = l.Session != nil && l.Session.LeaseExpiresAt <= now
	browserExpired = l.WS != nil && l.WSExpiresAt != nil && *l.WSExpiresAt <= now
	return browserExpired, restExpired
}

// resumeFrame builds the resume control frame ({"type":"control",...}) sent to
// a worker when a lease is released.
func resumeFrame(owner string, ts float64) map[string]any {
	return map[string]any{
		"type":    "control",
		"action":  "resume",
		"owner":   owner,
		"lease_s": 0,
		"ts":      ts,
	}
}

// pauseFrame builds the pause control frame sent to a worker on REST acquire.
func pauseFrame(owner, hijackID string, ts float64) map[string]any {
	return map[string]any{
		"type":      "control",
		"action":    "pause",
		"owner":     owner,
		"hijack_id": hijackID,
		"ts":        ts,
	}
}

// TouchOwner extends the dashboard-WS hijack lease, returning the new expiry
// or nil if there is no owner. leaseS nil uses the configured TTL; otherwise it
// is clamped into [1, 600].
func (lm *HijackLeaseManager) TouchOwner(workerID string, leaseS *int) *float64 {
	lm.lock.Lock()
	defer lm.lock.Unlock()
	st := lm.registry.Get(workerID)
	if st == nil || st.HijackOwner == nil {
		return nil
	}
	ttl := lm.dashboardLeaseS
	if leaseS != nil {
		ttl = clampDashboardLease(*leaseS)
	}
	exp := lm.clock.Monotonic() + float64(ttl)
	st.HijackOwnerExpiresAt = &exp
	return &exp
}

// TouchIfOwner verifies ws still owns the dashboard hijack and extends the
// lease, returning the new expiry or nil.
func (lm *HijackLeaseManager) TouchIfOwner(workerID string, ws BrowserConn) *float64 {
	lm.lock.Lock()
	defer lm.lock.Unlock()
	st := lm.registry.Get(workerID)
	if st == nil || !lm.hub.IsDashboardHijackActive(st) || st.HijackOwner != ws {
		return nil
	}
	exp := lm.clock.Monotonic() + float64(lm.dashboardLeaseS)
	st.HijackOwnerExpiresAt = &exp
	return &exp
}

// TryReleaseWs verifies ws owns the dashboard hijack and clears it in one lock
// block, returning (released, restActive).
func (lm *HijackLeaseManager) TryReleaseWs(workerID string, ws BrowserConn) (released, restActive bool) {
	lm.lock.Lock()
	st := lm.registry.Get(workerID)
	if st == nil || !lm.hub.IsDashboardHijackActive(st) || st.HijackOwner != ws {
		restActive := st != nil && lm.hub.HasValidRESTLease(st)
		lm.lock.Unlock()
		return false, restActive
	}
	st.HijackOwner = nil
	st.HijackOwnerExpiresAt = nil
	restActive = lm.hub.HasValidRESTLease(st)
	lm.lock.Unlock()
	lm.logger.Info(eventHijackReleased, "worker_id", workerID, "hijack_type", "dashboard")
	return true, restActive
}

// StillHijacked reports whether any hijack (REST or dashboard) is active.
func (lm *HijackLeaseManager) StillHijacked(workerID string) bool {
	lm.lock.Lock()
	defer lm.lock.Unlock()
	st := lm.registry.Get(workerID)
	if st == nil {
		return false
	}
	return lm.hub.IsHijacked(st)
}

// IsInputOpenMode reports whether the worker is in open input mode.
func (lm *HijackLeaseManager) IsInputOpenMode(workerID string) bool {
	lm.lock.Lock()
	defer lm.lock.Unlock()
	st := lm.registry.Get(workerID)
	return st != nil && st.InputMode == InputModeOpen
}

// PrepareBrowserInput reports whether ws may send input and, if ws is the
// active dashboard owner, extends the dashboard lease.
func (lm *HijackLeaseManager) PrepareBrowserInput(workerID string, ws BrowserConn) bool {
	lm.lock.Lock()
	defer lm.lock.Unlock()
	st := lm.registry.Get(workerID)
	if st == nil {
		return false
	}
	allowed := lm.hub.CanSendInput(st, ws)
	if lm.hub.IsDashboardHijackActive(st) && st.HijackOwner == ws {
		exp := lm.clock.Monotonic() + float64(lm.dashboardLeaseS)
		st.HijackOwnerExpiresAt = &exp
	}
	return allowed
}

// CheckValid reports whether the REST hijack session is still valid (matching
// id and unexpired at the current monotonic time).
func (lm *HijackLeaseManager) CheckValid(workerID, hijackID string) bool {
	lm.lock.Lock()
	defer lm.lock.Unlock()
	st := lm.registry.Get(workerID)
	return st != nil &&
		st.HijackSession != nil &&
		st.HijackSession.HijackID == hijackID &&
		st.HijackSession.LeaseExpiresAt > lm.clock.Monotonic()
}

// GetFreshExpiry re-reads the current lease expiry under lock, or returns
// fallback when there is no matching session.
func (lm *HijackLeaseManager) GetFreshExpiry(workerID, hijackID string, fallback float64) float64 {
	lm.lock.Lock()
	defer lm.lock.Unlock()
	st := lm.registry.Get(workerID)
	if st != nil && st.HijackSession != nil && st.HijackSession.HijackID == hijackID {
		return st.HijackSession.LeaseExpiresAt
	}
	return fallback
}
