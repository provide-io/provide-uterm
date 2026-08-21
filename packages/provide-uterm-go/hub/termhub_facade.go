//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/frames"
)

// This file holds the connection / router / presence facade delegators on
// TermHub — the cross-service orchestration bodies (telemetry emission, per-ws
// cleanup, event-bus close) that the Python core_delegates_connection /
// core_impl wrappers carried. Thin delegators without extra orchestration are
// grouped here too so callers have a single facade surface.

// -- Router facade -----------------------------------------------------------

// Broadcast sends msg to all browsers for workerID.
func (h *TermHub) Broadcast(ctx context.Context, workerID string, msg map[string]any) error {
	return h.Router.Broadcast(ctx, workerID, msg)
}

// AppendEventData appends an event (with data) and returns the stored row.
func (h *TermHub) AppendEventData(ctx context.Context, workerID, eventType string, data map[string]any) (map[string]any, error) {
	return h.Router.AppendEvent(ctx, workerID, eventType, data)
}

// HijackStateMsgFor builds a hijack_state frame for ws.
func (h *TermHub) HijackStateMsgFor(ctx context.Context, workerID string, ws BrowserConn) frames.HijackStateFrame {
	return h.Router.HijackStateMsgFor(ctx, workerID, ws)
}

// SetInputMode sets the worker input mode. Returns (ok, reason).
func (h *TermHub) SetInputMode(ctx context.Context, workerID, mode string) (bool, string, error) {
	return h.Router.SetInputMode(ctx, workerID, mode)
}

// GetLastSnapshot returns the most recent snapshot, redacted for recipient when
// an output gate is active.
func (h *TermHub) GetLastSnapshot(ctx context.Context, workerID string, recipient BrowserConn) (map[string]any, error) {
	return h.Router.GetLastSnapshot(ctx, workerID, recipient)
}

// GetRecentEvents returns up to limit recent events for workerID.
func (h *TermHub) GetRecentEvents(ctx context.Context, workerID string, limit int) []map[string]any {
	return h.Router.GetRecentEvents(ctx, workerID, limit)
}

// BrowserCount returns the number of browsers for workerID.
func (h *TermHub) BrowserCount(ctx context.Context, workerID string) int {
	return h.Router.BrowserCount(ctx, workerID)
}

// BrowserCountTotal returns the total browsers across all workers.
func (h *TermHub) BrowserCountTotal(ctx context.Context) int { return h.Router.BrowserCountTotal(ctx) }

// GetIdleCandidates returns browserless workers idle beyond timeoutS.
func (h *TermHub) GetIdleCandidates(ctx context.Context, timeoutS float64) []IdleCandidate {
	return h.Router.GetIdleCandidates(ctx, timeoutS)
}

// SetBrowserRole updates the role for ws.
func (h *TermHub) SetBrowserRole(ctx context.Context, workerID string, ws BrowserConn, role string) {
	h.Router.SetBrowserRole(ctx, workerID, ws, role)
}

// TryReclaimHijack acquires hijack ownership for ws when reclaimable.
func (h *TermHub) TryReclaimHijack(ctx context.Context, workerID string, ws BrowserConn) bool {
	return h.Router.TryReclaimHijack(ctx, workerID, ws)
}

// ReplaceBrowserResumeToken binds a successful resume's fresh token to ws.
func (h *TermHub) ReplaceBrowserResumeToken(ctx context.Context, ws BrowserConn, token string) error {
	return h.Conn.ReplaceBrowserResumeToken(ctx, ws, token)
}

// GetWorkerBrowserRole returns the role for ws (ok=false when unknown).
func (h *TermHub) GetWorkerBrowserRole(ctx context.Context, workerID string, ws BrowserConn) (string, bool) {
	return h.Router.GetWorkerBrowserRole(ctx, workerID, ws)
}

// -- Presence facade ---------------------------------------------------------

// RequestSnapshot pokes the worker for a fresh snapshot.
func (h *TermHub) RequestSnapshot(ctx context.Context, workerID string) error {
	return h.Presence.RequestSnapshot(ctx, workerID)
}

// RequestAnalysis pokes the worker for a fresh analysis result.
func (h *TermHub) RequestAnalysis(ctx context.Context, workerID string) error {
	return h.Presence.RequestAnalysis(ctx, workerID)
}

// CanSendInputTo reports whether ws may send input to st.
func (h *TermHub) CanSendInputTo(st *WorkerTermState, ws BrowserConn) bool {
	return h.Presence.CanSendInput(st, ws)
}

// ResolveRoleForBrowser resolves ws's role.
func (h *TermHub) ResolveRoleForBrowser(ctx context.Context, ws BrowserConn, workerID string) (string, error) {
	return h.Presence.ResolveRoleForBrowser(ctx, ws, workerID)
}

// RegisterBrowserStateSnapshot returns current browser state without registering.
func (h *TermHub) RegisterBrowserStateSnapshot(workerID string, ws BrowserConn) map[string]any {
	return h.Presence.RegisterBrowserStateSnapshot(workerID, ws)
}

// -- Rate-limit / token facade ----------------------------------------------

// AllowRESTAcquireFor gates a REST acquire.
func (h *TermHub) AllowRESTAcquireFor(clientID string) bool {
	return h.Conn.AllowRESTAcquireFor(clientID)
}

// AllowRESTSendFor gates a REST send.
func (h *TermHub) AllowRESTSendFor(clientID string) bool { return h.Conn.AllowRESTSendFor(clientID) }

// WorkerToken returns the configured worker bearer token.
func (h *TermHub) WorkerToken() *string { return h.Conn.WorkerToken() }

// -- Worker lifecycle facade -------------------------------------------------

// RegisterWorker registers ws as workerID's worker and emits telemetry.
func (h *TermHub) RegisterWorker(ctx context.Context, workerID string, ws WorkerWS) (bool, error) {
	result, err := h.Conn.RegisterWorker(ctx, workerID, ws)
	if err != nil {
		return false, err
	}
	h.emitTelemetry(ctx, "session.registered", workerID, nil, nil, map[string]any{"session_type": "worker"})
	return result, nil
}

// IsActiveWorker reports whether ws is still workerID's worker.
func (h *TermHub) IsActiveWorker(ctx context.Context, workerID string, ws WorkerWS) bool {
	return h.Conn.IsActiveWorker(ctx, workerID, ws)
}

// SetWorkerTunnelFlag marks whether workerID uses the tunnel wire format.
func (h *TermHub) SetWorkerTunnelFlag(ctx context.Context, workerID string, value bool) {
	h.Conn.SetWorkerTunnelFlag(ctx, workerID, value)
}

// RegisterWorkerWithTransport atomically registers a worker and its wire mode.
func (h *TermHub) RegisterWorkerWithTransport(
	ctx context.Context, workerID string, ws WorkerWS, isTunnel bool,
) (bool, error) {
	return h.Conn.RegisterWorkerWithTransport(ctx, workerID, ws, isTunnel)
}

// SetWorkerHello applies a worker_hello.
func (h *TermHub) SetWorkerHello(ctx context.Context, workerID, mode string, protocolVersion *int) (bool, error) {
	return h.Conn.SetWorkerHello(ctx, workerID, mode, protocolVersion)
}

// SetWorkerHelloMode is the string-typed wrapper around SetWorkerHello, rejecting
// an invalid mode. Port of core_orchestration.set_worker_hello_mode.
func (h *TermHub) SetWorkerHelloMode(ctx context.Context, workerID, mode string) (bool, error) {
	if mode != InputModeHijack && mode != InputModeOpen {
		return false, &InvalidInputModeError{Mode: mode}
	}
	return h.Conn.SetWorkerHello(ctx, workerID, mode, nil)
}

// HasWorkerSocket reports whether workerID has a live worker socket attached.
//
// It is the condition every hijack turns on — [HijackLeaseManager.TryAcquireRest]
// refuses with "no_worker" without it — so a caller that has just started a
// session can tell "attached, ready to be leased" from "registered but not yet
// connected". Read under the shared hub lock, like every other worker-state read.
func (h *TermHub) HasWorkerSocket(workerID string) bool {
	h.lock.Lock()
	defer h.lock.Unlock()
	st := h.registry.Get(workerID)
	return st != nil && st.WorkerWS != nil
}

// HasWorkerHello reports whether workerID's current socket has had its
// worker_hello processed, so the mode the hub holds is the one the worker
// announced rather than the "hijack" default it was created with.
//
// [HasWorkerSocket] answers "can this worker be reached"; this answers "is what
// the hub believes about it true yet". A caller that has just started a session
// needs both before it can call the session ready, because a lease taken in
// between is granted against the default rather than against the configuration.
func (h *TermHub) HasWorkerHello(workerID string) bool {
	h.lock.Lock()
	defer h.lock.Unlock()
	st := h.registry.Get(workerID)
	return st != nil && st.HelloApplied
}

// UpdateLastSnapshot stores the most recent snapshot for workerID.
func (h *TermHub) UpdateLastSnapshot(ctx context.Context, workerID string, snapshot map[string]any) {
	h.Conn.UpdateLastSnapshot(ctx, workerID, snapshot)
}

// DeregisterWorker clears ws and closes the EventBus stream on true disconnect.
func (h *TermHub) DeregisterWorker(ctx context.Context, workerID string, ws WorkerWS) (bool, bool) {
	shouldBroadcast, wasHijacked := h.Conn.DeregisterWorker(ctx, workerID, ws)
	if shouldBroadcast && h.eventBus != nil {
		h.eventBus.CloseWorker(workerID)
	}
	return shouldBroadcast, wasHijacked
}

// DisconnectWorker programmatically tears down workerID's worker socket.
func (h *TermHub) DisconnectWorker(ctx context.Context, workerID string) (bool, error) {
	return h.Conn.DisconnectWorker(ctx, workerID)
}

// ForceReleaseHijack forcibly clears any active hijack.
func (h *TermHub) ForceReleaseHijack(ctx context.Context, workerID string) (bool, error) {
	return h.Conn.ForceReleaseHijack(ctx, workerID)
}

// -- Browser lifecycle facade ------------------------------------------------

// RegisterBrowser registers ws as a browser and emits telemetry.
func (h *TermHub) RegisterBrowser(
	ctx context.Context, workerID string, ws BrowserConn, role string, deferBroadcast bool,
) (map[string]any, error) {
	result, err := h.Conn.RegisterBrowser(ctx, workerID, ws, role, deferBroadcast)
	if err != nil {
		return nil, err
	}
	h.emitTelemetry(ctx, "session.registered", workerID, nil, strp(role), map[string]any{"session_type": "browser"})
	return result, nil
}

// ActivateBrowserBroadcasts enables broadcasts to ws after startup.
func (h *TermHub) ActivateBrowserBroadcasts(ctx context.Context, workerID string, ws BrowserConn) {
	h.Conn.ActivateBrowserBroadcasts(ctx, workerID, ws)
}

// CleanupBrowserDisconnect fences the connection transition before clearing
// per-ws auxiliary state and emitting telemetry.
func (h *TermHub) CleanupBrowserDisconnect(
	ctx context.Context, workerID string, ws BrowserConn, ownedHijack bool,
) (map[string]any, error) {
	result, err := h.Conn.CleanupBrowserDisconnect(ctx, workerID, ws, ownedHijack)
	if err != nil {
		return nil, err
	}
	h.Router.ForgetBrowser(ws)
	h.State.ForgetInputBuffer(ws)
	h.lock.Lock()
	delete(h.holdBuffers, ws)
	delete(h.pausedBrowsers, ws)
	h.lock.Unlock()
	h.emitTelemetry(ctx, "session.disconnected", workerID, nil, nil, map[string]any{"session_type": "browser"})
	return result, nil
}

// RemoveDeadBrowsers clears per-ws state for dead browsers then calls the lease
// manager. Port of core_delegates_connection.remove_dead_browsers.
func (h *TermHub) RemoveDeadBrowsers(ctx context.Context, workerID string, dead []BrowserConn) (bool, error) {
	changed, err := h.Lease.RemoveDeadBrowsers(ctx, workerID, dead)
	if err != nil {
		return false, err
	}
	h.lock.Lock()
	for _, ws := range dead {
		delete(h.holdBuffers, ws)
		delete(h.startupPendingBrowsers, ws)
		delete(h.startupPendingFrames, ws)
		delete(h.pausedBrowsers, ws)
	}
	h.lock.Unlock()
	for _, ws := range dead {
		h.Router.ForgetBrowser(ws)
		h.State.ForgetInputBuffer(ws)
	}
	return changed, nil
}

// spawnWorkerEmpty fires the on-worker-empty callback as a tracked background
// task (fire-and-forget, decoupled from the request context). Port of the
// asyncio.create_task(on_empty(worker_id)) branch in cleanup_browser_disconnect.
func (h *TermHub) spawnWorkerEmpty(_ context.Context, workerID string) {
	cctx, cancel := context.WithCancel(context.Background())
	res := make(chan error, 1)
	go func() { res <- h.onWorkerEmpty(cctx, workerID) }()
	h.tasks.Add(cancel, res)
}
