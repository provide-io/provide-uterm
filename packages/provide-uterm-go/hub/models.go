//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"log/slog"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/gui"
)

// Input modes for a worker. Mirrors provide.uterm.bridge.contracts.InputMode.
const (
	InputModeHijack = "hijack"
	InputModeOpen   = "open"
)

// BrowserConn identifies a dashboard browser WebSocket. The Python code stores
// the FastAPI WebSocket object and compares it by identity (“is“) and uses it
// as a dict key. In Go it is any comparable value (typically a pointer); the
// services compare it with == and use it as a map key, matching the Python
// identity semantics.
type BrowserConn = any

// WorkerWS is the worker-side transport the lease manager pauses when a REST
// hijack is acquired. It is the minimal surface of the Python worker
// WebSocket used by the ported services (only “send_text“).
type WorkerWS interface {
	// SendText writes an already-encoded frame to the worker. The Python
	// path (``worker_ws.send_text``) is async; a returned error models a
	// dead/backpressured socket.
	SendText(ctx context.Context, payload string) error
}

// HijackSession is a live REST hijack lease. Port of
// provide.uterm.bridge.coordinator.HijackSession.
type HijackSession struct {
	HijackID       string
	Owner          string
	LeaseExpiresAt float64
	AcquiredAt     float64
	LastHeartbeat  float64
	// AcquiredBy is the authenticated subject_id of the acquiring principal
	// (nil for unauthenticated/legacy leases).
	AcquiredBy *string
}

// HijackLease is a view object over the three hijack fields on a
// [WorkerTermState]. Port of provide.uterm.server.bridge.models.HijackLease.
//
// It borrows the slots rather than owning them: a fresh HijackLease is
// produced by [WorkerTermState.Lease] each call, and [WorkerTermState.ApplyLease]
// writes a mutated view back. Methods take “now“ explicitly to stay
// deterministic.
type HijackLease struct {
	WS          BrowserConn
	WSExpiresAt *float64
	Session     *HijackSession
}

// IsIdle reports whether neither the dashboard-WS nor the REST slot is occupied.
func (l HijackLease) IsIdle() bool { return l.WS == nil && l.Session == nil }

// IsDashboardActive reports whether the dashboard-WS slot is occupied and unexpired at now.
func (l HijackLease) IsDashboardActive(now float64) bool {
	if l.WS == nil || l.WSExpiresAt == nil {
		return false
	}
	return *l.WSExpiresAt > now
}

// IsRESTActive reports whether the REST slot is occupied and unexpired at now.
func (l HijackLease) IsRESTActive(now float64) bool {
	if l.Session == nil {
		return false
	}
	return l.Session.LeaseExpiresAt > now
}

// IsActive reports whether either sub-lease is active at now.
func (l HijackLease) IsActive(now float64) bool {
	return l.IsDashboardActive(now) || l.IsRESTActive(now)
}

// Expire clears any expired sub-lease and returns (restExpired, dashExpired).
// A sub-lease is expired only when its expiry timestamp is at/before now AND
// the slot is occupied; idle slots return false (clearing nothing is not an
// expiry event for telemetry).
func (l *HijackLease) Expire(now float64) (restExpired, dashExpired bool) {
	restExpired = l.Session != nil && l.Session.LeaseExpiresAt <= now
	dashExpired = l.WS != nil && l.WSExpiresAt != nil && *l.WSExpiresAt <= now
	if restExpired {
		l.Session = nil
	}
	if dashExpired {
		l.WS = nil
		l.WSExpiresAt = nil
	}
	return restExpired, dashExpired
}

// WorkerTermState is the per-worker connection state held by the registry.
// Port of provide.uterm.server.bridge.models.WorkerTermState.
//
// All field access is expected to be serialised by the composing hub's shared
// mutex (matching the Python “TermHub._lock“ invariant); the struct itself
// takes no locks.
type WorkerTermState struct {
	WorkerWS             WorkerWS
	Browsers             map[BrowserConn]string
	HijackOwner          BrowserConn
	HijackOwnerExpiresAt *float64
	HijackSession        *HijackSession
	// HijackPending is a transient REST-acquire reservation held while the
	// worker is paused OUTSIDE the hub lock. See
	// [HijackLeaseManager.TryAcquireRest].
	HijackPending   *string
	InputMode       string
	LastSnapshot    map[string]any
	Events          []map[string]any
	EventSeq        int
	MinEventSeq     int
	LastActivityAt  float64
	ProtocolVersion *int
	IsTunnelWorker  bool
	GraphicalSession gui.GraphicalSession
}

// NewWorkerTermState creates a worker state with the Python dataclass
// defaults (empty browser map, "hijack" input mode).
func NewWorkerTermState() *WorkerTermState {
	return &WorkerTermState{
		Browsers:  map[BrowserConn]string{},
		InputMode: InputModeHijack,
	}
}

// Lease constructs a fresh [HijackLease] view over this state's hijack fields.
// Mutations on the returned view do NOT propagate back; use [WorkerTermState.ApplyLease].
func (st *WorkerTermState) Lease() HijackLease {
	return HijackLease{
		WS:          st.HijackOwner,
		WSExpiresAt: st.HijackOwnerExpiresAt,
		Session:     st.HijackSession,
	}
}

// ApplyLease writes a [HijackLease] view back onto this state's hijack fields.
func (st *WorkerTermState) ApplyLease(l HijackLease) {
	st.HijackOwner = l.WS
	st.HijackOwnerExpiresAt = l.WSExpiresAt
	st.HijackSession = l.Session
}

// loggerOrDefault returns l, or slog.Default() when l is nil.
func loggerOrDefault(l *slog.Logger) *slog.Logger {
	if l == nil {
		return slog.Default()
	}
	return l
}

// f64p returns a pointer to v — a convenience for optional float fields.
func f64p(v float64) *float64 { return &v }

// strp returns a pointer to v — a convenience for optional string fields.
func strp(v string) *string { return &v }
