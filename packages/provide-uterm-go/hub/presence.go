//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"crypto/rand"
	"fmt"
)

// PresenceManager owns the read-only browser-presence queries and the
// worker-bound presence control frames. Port of
// provide.uterm.server.bridge.hub.presence.PresenceManager.
//
// It holds a back reference to the composing [TermHub] for the small set of
// cross-cutting queries it needs (hijack-state predicates, the role-resolver
// callback, send_worker) — mirroring the Python back-reference exactly. Lock
// semantics are preserved: it uses the hub's shared mutex.
type PresenceManager struct {
	hub *TermHub
}

// newPresenceManager builds a manager bound to hub.
func newPresenceManager(hub *TermHub) *PresenceManager { return &PresenceManager{hub: hub} }

// RegisterBrowserStateSnapshot returns the current browser state without
// re-registering (used after a resume to get updated hello fields). Port of
// register_browser_state_snapshot.
func (p *PresenceManager) RegisterBrowserStateSnapshot(worker_id string, ws BrowserConn) map[string]any {
	hub := p.hub
	hub.lock.Lock()
	defer hub.lock.Unlock()
	st := hub.registry.Get(worker_id)
	if st == nil {
		return map[string]any{
			"is_hijacked":    false,
			"hijacked_by_me": false,
			"worker_online":  false,
			"input_mode":     "hijack",
		}
	}
	return map[string]any{
		"is_hijacked":    hub.State.IsHijacked(st),
		"hijacked_by_me": hub.State.IsDashboardHijackActive(st) && st.HijackOwner == ws,
		"worker_online":  st.WorkerWS != nil,
		"input_mode":     st.InputMode,
	}
}

// ResolveRoleForBrowser is the public wrapper around the hub's role-resolver
// callback. Port of resolve_role_for_browser.
func (p *PresenceManager) ResolveRoleForBrowser(ctx context.Context, ws BrowserConn, worker_id string) (string, error) {
	return p.hub.State.ResolveRoleForBrowser(ctx, ws, worker_id)
}

// CanSendInput reports whether ws may send input to the worker. Port of
// can_send_input. In open mode only operators/admins may send (viewers are
// excluded); otherwise the ws must be the active dashboard hijack owner.
//
// This is the per-input-frame hot path and deliberately takes no lock — it
// operates on an already-captured [WorkerTermState] reference.
func (p *PresenceManager) CanSendInput(st *WorkerTermState, ws BrowserConn) bool {
	if st.InputMode == InputModeOpen {
		role := st.Browsers[ws]
		if role == "" {
			role = "viewer"
		}
		return role == "operator" || role == "admin"
	}
	return p.hub.State.IsDashboardHijackActive(st) && st.HijackOwner == ws
}

// RequestSnapshot sends a snapshot_req control frame to the worker (no-op if no
// worker is connected). Port of request_snapshot.
func (p *PresenceManager) RequestSnapshot(ctx context.Context, worker_id string) error {
	_, err := p.hub.SendWorker(ctx, worker_id, map[string]any{
		"type":   "snapshot_req",
		"req_id": p.hub.newID(),
		"ts":     p.hub.clock.Wall(),
	})
	return err
}

// RequestAnalysis sends an analyze_req control frame to the worker (no-op if no
// worker is connected). Port of request_analysis.
func (p *PresenceManager) RequestAnalysis(ctx context.Context, worker_id string) error {
	_, err := p.hub.SendWorker(ctx, worker_id, map[string]any{
		"type":   "analyze_req",
		"req_id": p.hub.newID(),
		"ts":     p.hub.clock.Wall(),
	})
	return err
}

// newUUID4 returns a random RFC-4122 v4 UUID string (the Go analogue of
// uuid.uuid4()). It is the default id generator for presence req_id fields.
func newUUID4() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil { //nolint:staticcheck // rand.Read never errors on supported platforms
		panic(err)
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}
