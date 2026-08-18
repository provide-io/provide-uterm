//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "context"

// RegisterBrowser registers ws as a browser for workerID and returns the
// initial state map. Port of register_browser.
//
// The per-principal quota gate runs BEFORE minting the resume token so a
// rejected connection never orphans a token. On any error after the quota
// increment the increment is rolled back (mirroring the Python try/except).
// Returns a [WebSocketRejection] (code 1008) when the principal is at its
// connection cap.
func (c *ConnectionManager) RegisterBrowser(
	ctx context.Context, workerID string, ws BrowserConn, role string, deferBroadcast bool,
) (map[string]any, error) {
	hub := c.hub
	var resumeToken string
	hub.lock.Lock()

	subjectID := browserPrincipalSubjectID(ws)
	if subjectID != nil {
		current := hub.principalBrowserCounts[*subjectID]
		if current >= hub.maxConnectionsPerPrincipal {
			hub.lock.Unlock()
			return nil, &WebSocketRejection{Code: 1008, Reason: "too many connections"}
		}
		hub.principalBrowserCounts[*subjectID] = current + 1
		hub.wsPrincipal[ws] = *subjectID
	}

	// Everything past the increment is guarded so a failure rolls the quota
	// slot back (mirroring the Python try/except BaseException).
	if hub.resumeStore != nil {
		token, err := hub.resumeStore.Create(ctx, workerID, role, hub.resumeTTLS)
		if err != nil {
			c.rollbackBrowserQuota(ws)
			hub.lock.Unlock()
			return nil, err
		}
		resumeToken = token
		hub.wsToResumeToken[ws] = token
		hub.resumeTokenDetached[token] = make(chan struct{})
	}
	st := hub.registry.SetDefault(workerID, NewWorkerTermState())
	st.Browsers[ws] = role
	if deferBroadcast {
		hub.startupPendingBrowsers[ws] = true
	}
	var resumeTokenAny any
	if resumeToken != "" {
		resumeTokenAny = resumeToken
	}
	initialState := map[string]any{
		"is_hijacked":      hub.State.IsHijacked(st),
		"hijacked_by_me":   hub.State.IsDashboardHijackActive(st) && st.HijackOwner == ws,
		"worker_online":    st.WorkerWS != nil,
		"input_mode":       st.InputMode,
		"initial_snapshot": st.LastSnapshot,
		"resume_token":     resumeTokenAny,
	}
	hub.lock.Unlock()

	// Redact the connect-time snapshot OUTSIDE the lock (the policy context
	// build re-acquires the hub lock). redact returns a COPY.
	if snap, _ := initialState["initial_snapshot"].(map[string]any); snap != nil && hub.outputPolicyGate != nil {
		redacted, err := hub.Router.RedactSnapshotForRecipient(ctx, workerID, snap, ws)
		if err != nil {
			return nil, err
		}
		initialState["initial_snapshot"] = redacted
	}
	hub.logger.Info(eventSessionRegistered, "worker_id", workerID, "session_type", "browser", "role", role)
	return initialState, nil
}

// ReplaceBrowserResumeToken makes the post-resume token the sole token tied to
// ws, revoking the connect-time token that it supersedes.
func (c *ConnectionManager) ReplaceBrowserResumeToken(ctx context.Context, ws BrowserConn, token string) error {
	hub := c.hub
	hub.lock.Lock()
	previous := hub.wsToResumeToken[ws]
	if previous != "" && previous != token {
		hub.detachResumeTokenLocked(previous)
	}
	hub.wsToResumeToken[ws] = token
	if _, exists := hub.resumeTokenDetached[token]; !exists {
		hub.resumeTokenDetached[token] = make(chan struct{})
	}
	hub.lock.Unlock()
	if previous != "" && previous != token && hub.resumeStore != nil {
		return hub.resumeStore.Revoke(ctx, previous)
	}
	return nil
}

// CleanupBrowserDisconnect handles a browser WS disconnect atomically. Port of
// cleanup_browser_disconnect. Returns a map with was_owner, rest_still_active,
// resume_without_owner.
func (c *ConnectionManager) CleanupBrowserDisconnect(
	ctx context.Context, workerID string, ws BrowserConn, ownedHijack bool,
) (map[string]any, error) {
	hub := c.hub
	opCtx, cancel := boundedOperationContext(ctx)
	defer cancel()
	browserCount := -1
	wasOwner, restStillActive, resumeWithoutOwner := false, false, false
	resumeSent := false
	var lifecycle *LifecycleReservation

	token := ""
	for {
		hub.lock.Lock()
		st := hub.registry.Get(workerID)
		if done := statePendingDone(st, true); done != nil {
			hub.lock.Unlock()
			if err := waitInputReservation(opCtx, done); err != nil {
				return nil, err
			}
			continue
		}
		if st != nil {
			_, stillRegistered := st.Browsers[ws]
			currentOwner := hub.State.IsDashboardHijackActive(st) && st.HijackOwner == ws
			if currentOwner {
				if err := hub.markBrowserResumeOwnerLocked(opCtx, ws, true); err != nil {
					hub.lock.Unlock()
					return nil, err
				}
			}
			wasOwner, restStillActive, resumeWithoutOwner = c.updateLockState(st, ws, ownedHijack && stillRegistered)
			browserCount = len(st.Browsers)
			if (wasOwner && !restStillActive) || resumeWithoutOwner {
				lifecycle = hub.beginLifecycleLocked(st, "browser_disconnect_resume")
			}
		}
		// Pop the resume token + startup-pending flag under the lock (these
		// hub-level maps are lock-guarded in this port).
		if hub.resumeStore != nil {
			token = hub.wsToResumeToken[ws]
			delete(hub.wsToResumeToken, ws)
			hub.detachResumeTokenLocked(token)
		}
		delete(hub.startupPendingBrowsers, ws)
		// Nothing will ever flush this socket's backlog now.
		delete(hub.startupPendingFrames, ws)
		hub.lock.Unlock()
		break
	}

	if lifecycle != nil {
		defer hub.finishLifecycle(workerID, lifecycle)
		if sent, err := hub.SendWorker(opCtx, workerID, resumeFrame("dashboard", hub.clock.Wall())); err != nil {
			return nil, err
		} else {
			resumeSent = sent
		}
		if resumeSent {
			hub.NotifyHijackChanged(workerID, false, nil)
		}
	}

	// Fire the empty-browser callback when the last browser left.
	if browserCount == 0 && hub.onWorkerEmpty != nil {
		hub.spawnWorkerEmpty(ctx, workerID)
	}
	hub.logger.Info(eventSessionDisconnected, "worker_id", workerID, "session_type", "browser")
	return map[string]any{
		"was_owner":            wasOwner,
		"rest_still_active":    restStillActive,
		"resume_without_owner": resumeWithoutOwner,
		"resume_sent":          resumeSent,
	}, nil
}
