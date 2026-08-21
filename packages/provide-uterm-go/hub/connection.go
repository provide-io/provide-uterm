//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "context"

// ConnectionManager owns the worker/browser connection-churn surface: the REST
// rate-limit gates, worker WS lifecycle (register/deregister/hello/tunnel-flag/
// snapshot), browser WS lifecycle (register/activate/disconnect with the
// per-principal quota + resume-token minting), and the hijack-clearing
// lifecycle (disconnect_worker / force_release_hijack in connection_hijack.go).
// Port of provide.uterm.server.bridge.hub.connection.ConnectionManager.
//
// It holds a back reference to the composing [TermHub] and uses the hub's
// shared mutex, preserving the Python lock semantics verbatim.
type ConnectionManager struct {
	hub *TermHub
}

func newConnectionManager(hub *TermHub) *ConnectionManager { return &ConnectionManager{hub: hub} }

// -- Rate limiting -----------------------------------------------------------

// AllowRESTAcquireFor gates a REST hijack-acquire, logging a rejection. Port of
// allow_rest_acquire_for.
func (c *ConnectionManager) AllowRESTAcquireFor(clientID string) bool {
	allowed := c.hub.Limiter.AllowRESTAcquire(clientID)
	if !allowed {
		c.hub.logger.Warn(eventRateLimitTriggered, "client_id", clientID, "limit_type", "rest_acquire")
	}
	return allowed
}

// AllowRESTSendFor gates a REST send/step, logging a rejection. Port of
// allow_rest_send_for.
func (c *ConnectionManager) AllowRESTSendFor(clientID string) bool {
	allowed := c.hub.Limiter.AllowRESTSend(clientID)
	if !allowed {
		c.hub.logger.Warn(eventRateLimitTriggered, "client_id", clientID, "limit_type", "rest_send")
	}
	return allowed
}

// WorkerToken returns the configured worker bearer token. Port of worker_token.
func (c *ConnectionManager) WorkerToken() *string { return c.hub.workerToken }

// -- Worker connection lifecycle ---------------------------------------------

// RegisterWorker registers ws as the active worker for workerID, clearing stale
// (expired) hijack state from a prior session. Port of register_worker. Returns
// (prevWasHijacked, error); a full worker map for a brand-new worker id yields a
// [WebSocketRejection] with code 1008.
func (c *ConnectionManager) RegisterWorker(ctx context.Context, workerID string, ws WorkerWS) (bool, error) {
	return c.RegisterWorkerWithTransport(ctx, workerID, ws, false)
}

// RegisterWorkerWithTransport publishes the worker identity and wire codec in
// one lock transition, so no input reservation can capture a mismatched codec.
func (c *ConnectionManager) RegisterWorkerWithTransport(
	ctx context.Context, workerID string, ws WorkerWS, isTunnel bool,
) (bool, error) {
	hub := c.hub
	for {
		hub.lock.Lock()
		if !hub.registry.Contains(workerID) && hub.registry.Len() >= hub.maxWorkers {
			hub.lock.Unlock()
			return false, &WebSocketRejection{Code: 1008, Reason: "worker capacity exceeded"}
		}
		st := hub.registry.SetDefault(workerID, NewWorkerTermState())
		if done := statePendingDone(st, true); done != nil {
			hub.lock.Unlock()
			if err := waitInputReservation(ctx, done); err != nil {
				return false, err
			}
			continue
		}
		if hub.State.HasValidRESTLease(st) {
			hub.lock.Unlock()
			return false, &WebSocketRejection{Code: 1008, Reason: "worker has active REST hijack"}
		}
		if len(st.Events) > hub.eventDequeMaxlen {
			st.Events = st.Events[len(st.Events)-hub.eventDequeMaxlen:]
		}
		now := hub.clock.Monotonic()
		expired := st.HijackSession != nil && st.HijackSession.LeaseExpiresAt <= now
		prevWasHijacked := expired || (st.HijackSession == nil && st.HijackOwner != nil)
		if expired {
			st.HijackSession = nil
		}
		if prevWasHijacked {
			st.clearDashboardOwner()
		}
		st.WorkerWS = ws
		st.IsTunnelWorker = isTunnel
		// A fresh socket has not announced its mode yet, whatever the previous
		// one said. See WorkerTermState.HelloApplied.
		st.HelloApplied = false
		st.WorkerGeneration++
		hub.lock.Unlock()
		hub.logger.Info(eventSessionRegistered, "worker_id", workerID, "session_type", "worker")
		return prevWasHijacked, nil
	}
}

// IsActiveWorker reports whether ws is still the registered worker. Port of
// is_active_worker.
func (c *ConnectionManager) IsActiveWorker(_ context.Context, workerID string, ws WorkerWS) bool {
	hub := c.hub
	hub.lock.Lock()
	defer hub.lock.Unlock()
	st := hub.registry.Get(workerID)
	return st != nil && st.WorkerWS == ws
}

// SetWorkerTunnelFlag marks whether workerID's worker WS uses the tunnel wire
// format. Port of set_worker_tunnel_flag.
func (c *ConnectionManager) SetWorkerTunnelFlag(_ context.Context, workerID string, value bool) {
	hub := c.hub
	hub.lock.Lock()
	defer hub.lock.Unlock()
	if st := hub.registry.Get(workerID); st != nil {
		st.IsTunnelWorker = value
	}
}

// SetWorkerHello applies a worker_hello: sets input_mode and records the
// protocol version. Port of set_worker_hello. Returns false when the worker is
// unknown or when switching to "open" while a hijack is active.
func (c *ConnectionManager) SetWorkerHello(ctx context.Context, workerID, mode string, protocolVersion *int) (bool, error) {
	hub := c.hub
	opCtx, cancel := boundedOperationContext(ctx)
	defer cancel()
	if protocolVersion != nil {
		hub.logger.Info("worker_hello_protocol", "worker_id", workerID, "version", *protocolVersion)
		if *protocolVersion < 1 {
			hub.logger.Warn("worker_hello_legacy_protocol", "worker_id", workerID, "version", *protocolVersion)
		}
	}
	for {
		hub.lock.Lock()
		st := hub.registry.Get(workerID)
		if st == nil {
			hub.lock.Unlock()
			return false, nil
		}
		if done := statePendingDone(st, true); done != nil {
			hub.lock.Unlock()
			if err := waitInputReservation(opCtx, done); err != nil {
				return false, err
			}
			continue
		}
		// A hello may raise the mode, never lower it. Two reasons to refuse, both
		// needed: a lease is actually held, or somebody has explicitly decided the
		// mode through an authenticated route. The second is the window the lease
		// check alone left open — an operator sets hijack and then acquires, and a
		// hello landing between those two steps reverted the mode, so the acquire was
		// refused for being in open mode and the operator's only clue was a failure
		// that looked like their own mistake.
		//
		// Keyed on whether the hello would actually lower the mode, not on its value:
		// a hello agreeing with a decided "open" is not a downgrade. And the decision
		// flag is what makes this expressible at all, since InputMode defaults to
		// hijack and refusing every lowering would refuse every worker announcing
		// open.
		wouldLower := mode == InputModeOpen && st.InputMode == InputModeHijack
		if wouldLower && (st.InputModeSetByOperator || hub.State.IsHijacked(st) || st.HijackPending != nil) {
			hub.logger.Warn("worker_hello_mode_blocked", "worker_id", workerID)
			// Refusing the mode is still a decision about it, so the worker has
			// finished announcing itself and readiness must not wait longer.
			st.HelloApplied = true
			hub.lock.Unlock()
			return false, nil
		}
		st.InputMode = mode
		st.HelloApplied = true
		if protocolVersion != nil {
			st.ProtocolVersion = protocolVersion
		}
		hub.lock.Unlock()
		return true, nil
	}
}

// UpdateLastSnapshot stores snapshot as the most recent snapshot for workerID.
// Port of update_last_snapshot.
func (c *ConnectionManager) UpdateLastSnapshot(_ context.Context, workerID string, snapshot map[string]any) {
	hub := c.hub
	hub.lock.Lock()
	defer hub.lock.Unlock()
	if st := hub.registry.Get(workerID); st != nil {
		st.LastSnapshot = snapshot
	}
}

// DeregisterWorker clears ws as the active worker if it is still current. Port
// of deregister_worker. Returns (shouldBroadcastDisconnect, wasHijacked).
func (c *ConnectionManager) DeregisterWorker(ctx context.Context, workerID string, ws WorkerWS) (bool, bool) {
	hub := c.hub
	for {
		hub.lock.Lock()
		st := hub.registry.Get(workerID)
		if st == nil || st.WorkerWS != ws {
			hub.lock.Unlock()
			return false, false
		}
		if done := statePendingDone(st, true); done != nil {
			hub.lock.Unlock()
			if waitInputReservation(ctx, done) != nil {
				return false, false
			}
			continue
		}
		wasHijacked := st.HijackSession != nil || st.HijackOwner != nil
		st.WorkerWS = nil
		st.WorkerGeneration++
		st.HijackSession = nil
		st.clearDashboardOwner()
		hub.lock.Unlock()
		return true, wasHijacked
	}
}

// -- Browser connection lifecycle --------------------------------------------

// browserPrincipalSubjectID returns the principal subject_id for quota tracking,
// or nil for an exempt connection (no principal, or the "anonymous" subject).
// Port of _browser_principal_subject_id.
func browserPrincipalSubjectID(ws BrowserConn) *string {
	pc, ok := ws.(principalCarrier)
	if !ok {
		return nil
	}
	principal := pc.UtermPrincipal()
	pr, ok := principal.(*Principal)
	if !ok || pr == nil {
		return nil
	}
	if pr.SubjectID == "" || pr.SubjectID == "anonymous" {
		return nil
	}
	return strp(pr.SubjectID)
}

// ActivateBrowserBroadcasts releases a browser from its startup window,
// delivering what it missed first. Port of activate_browser_broadcasts.
//
// The socket stays pending until its queue is empty, so a frame broadcast
// while the flush is in flight is buffered behind the ones already waiting
// instead of overtaking them. Only when nothing is left does the browser join
// the normal broadcast set.
func (c *ConnectionManager) ActivateBrowserBroadcasts(ctx context.Context, workerID string, ws BrowserConn) {
	hub := c.hub
	for {
		hub.lock.Lock()
		batch := hub.startupPendingFrames[ws]
		if len(batch) == 0 {
			delete(hub.startupPendingFrames, ws)
			st := hub.registry.Get(workerID)
			// Guard preserved from before this buffered: a browser that
			// disconnected mid-startup is left pending on purpose.
			if st != nil {
				if _, ok := st.Browsers[ws]; ok {
					delete(hub.startupPendingBrowsers, ws)
				}
			}
			hub.lock.Unlock()
			return
		}
		hub.startupPendingFrames[ws] = nil
		hub.lock.Unlock()

		for _, buffered := range batch {
			payload, err := encodeBrowserFrame(buffered)
			if err == nil {
				err = sendToBrowser(ctx, ws, payload)
			}
			if err != nil {
				// A socket that cannot take its own backlog is gone. Drop the
				// backlog, but leave it PENDING: pending means the broadcast
				// path skips it, which is what you want for a socket that just
				// failed a write. The disconnect path removes it from both.
				hub.logger.Warn("startup_frame_flush_failed", "worker_id", workerID, "error", err)
				hub.lock.Lock()
				delete(hub.startupPendingFrames, ws)
				hub.lock.Unlock()
				return
			}
		}
	}
}

// rollbackBrowserQuota undoes the per-principal quota increment for ws (caller
// holds the lock). Port of _rollback_browser_quota.
func (c *ConnectionManager) rollbackBrowserQuota(ws BrowserConn) {
	hub := c.hub
	delete(hub.wsToResumeToken, ws)
	subjectID, ok := hub.wsPrincipal[ws]
	if !ok {
		return
	}
	delete(hub.wsPrincipal, ws)
	remaining := hub.principalBrowserCounts[subjectID] - 1
	if remaining <= 0 {
		delete(hub.principalBrowserCounts, subjectID)
	} else {
		hub.principalBrowserCounts[subjectID] = remaining
	}
}

// scanEventsForResume scans event history to decide if a resume frame is still
// needed on browser disconnect. Port of _scan_events_for_resume.
func scanEventsForResume(st *WorkerTermState) bool {
	for i := len(st.Events) - 1; i >= 0; i-- {
		t, ok := st.Events[i]["type"].(string)
		if !ok {
			continue
		}
		if t == "hijack_owner_expired" || t == "hijack_lease_expired" {
			return false
		}
		if t == "hijack_acquired" || t == "hijack_released" {
			break
		}
	}
	return true
}

// updateLockState applies disconnect mutations to st and returns
// (wasOwner, restStillActive, resumeWithoutOwner). Caller holds the lock.
// Port of _update_lock_state.
func (c *ConnectionManager) updateLockState(st *WorkerTermState, ws BrowserConn, ownedHijack bool) (bool, bool, bool) {
	hub := c.hub
	wasOwner := hub.State.IsDashboardHijackActive(st) && st.HijackOwner == ws
	restStillActive := false
	resumeWithoutOwner := false
	delete(st.Browsers, ws)
	if subjectID, ok := hub.wsPrincipal[ws]; ok {
		delete(hub.wsPrincipal, ws)
		remaining := hub.principalBrowserCounts[subjectID] - 1
		if remaining <= 0 {
			delete(hub.principalBrowserCounts, subjectID)
		} else {
			hub.principalBrowserCounts[subjectID] = remaining
		}
	}
	switch {
	case wasOwner:
		st.clearDashboardOwner()
		restStillActive = hub.State.HasValidRESTLease(st)
	case ownedHijack && st.WorkerWS != nil && !hub.State.IsHijacked(st):
		resumeWithoutOwner = scanEventsForResume(st)
	}
	return wasOwner, restStillActive, resumeWithoutOwner
}
