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
	// HijackOwnerGeneration changes whenever dashboard ownership changes. It
	// fences delayed policy/approval work against release-and-reacquire ABA.
	HijackOwnerGeneration uint64
	HijackSession         *HijackSession
	// HijackPending is a transient REST-acquire reservation held while the
	// worker is paused OUTSIDE the hub lock. See
	// [HijackLeaseManager.TryAcquireRest].
	HijackPending *string
	// InputSendPending reserves the exact owner/lease generation and worker
	// while one authorized input is being delivered outside the hub lock.
	// Ownership, expiry, disconnect, and worker-replacement transitions wait for
	// Done before mutating the state, making authorization + delivery linear.
	InputSendPending *InputSendReservation
	// LifecyclePending fences pause/resume and connection churn. Acquires,
	// input, and worker replacement wait for Done before observing new state.
	LifecyclePending *LifecycleReservation
	InputMode        string
	// InputModeSetByOperator records whether an authenticated caller has
	// explicitly decided this session's input mode, as opposed to it merely
	// holding the "hijack" default.
	//
	// It exists to tell two claims apart: a worker_hello announces what the
	// worker process booted with, while SetInputMode is a decision made through
	// an authenticated route by somebody holding session.control.mode. Without
	// the distinction the hub cannot refuse a hello that lowers hijack to open,
	// because InputMode defaults to hijack and refusing every lowering would
	// refuse every worker that legitimately announces open.
	//
	// Held on the worker state rather than the connection deliberately: registry
	// state outlives a worker socket, so a decision survives a reconnect.
	// Internal only — nothing serialises it onto the wire.
	InputModeSetByOperator bool
	// HelloApplied records whether this worker's connection has got as far as
	// its worker_hello being processed, in the sense that the hub has decided
	// what to do with the mode it announced — applied it, or deliberately kept
	// its own.
	//
	// It exists because a worker socket and the mode that socket speaks for
	// arrive at different moments: RegisterWorker attaches WorkerWS, and the
	// hello that says "open" is a frame read later, off the receive loop. In
	// between, InputMode still holds the "hijack" default, so a lease asked for
	// in that window is granted on a session that is configured open. Readiness
	// gates on this rather than on the socket alone.
	//
	// Cleared when a socket registers, because a reconnecting worker has to say
	// it again — the mode belongs to the connection, even though the operator's
	// decision above it does not.
	HelloApplied     bool
	LastSnapshot     map[string]any
	Events           []map[string]any
	EventSeq         int
	MinEventSeq      int
	LastActivityAt   float64
	ProtocolVersion  *int
	IsTunnelWorker   bool
	WorkerGeneration uint64
	GraphicalSession gui.GraphicalSession
}

// InputSendReservation is the in-flight state-owner fence for one worker
// delivery. It is stored only while a single browser or REST hijack input is
// being sent; Done is closed after delivery state has been reconciled.
type InputSendReservation struct {
	Worker           WorkerWS
	WorkerGeneration uint64
	IsTunnel         bool
	Done             chan struct{}
}

type LifecycleReservation struct {
	Kind             string
	Worker           WorkerWS
	WorkerGeneration uint64
	Done             chan struct{}
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
	if st.HijackOwner != l.WS {
		st.HijackOwnerGeneration++
	}
	st.HijackOwner = l.WS
	st.HijackOwnerExpiresAt = l.WSExpiresAt
	st.HijackSession = l.Session
}

func (st *WorkerTermState) setDashboardOwner(ws BrowserConn, expiresAt *float64) {
	if st.HijackOwner != ws {
		st.HijackOwnerGeneration++
	}
	st.HijackOwner = ws
	st.HijackOwnerExpiresAt = expiresAt
}

func (st *WorkerTermState) clearDashboardOwner() {
	st.setDashboardOwner(nil, nil)
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
