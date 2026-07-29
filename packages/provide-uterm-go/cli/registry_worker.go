//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/bridge"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
)

// The hub link: what turns a configured session into a worker.
//
// A session the server hosts is two things at once — a live connector holding a
// terminal open, and a worker the hub can address. The reference builds both in
// one place (HostedSessionRuntime._run: start the connector, then dial its own
// /ws/worker/{session_id}/term), and everything the hub does to a session goes
// through the second one. The lease manager, in particular, refuses to grant a
// hijack on a worker it has no socket to (hub.HijackLeaseManager.TryAcquireRest
// returns "no_worker"), because a lease that could not pause the worker would
// let an operator type into a terminal that automation was still driving.
//
// This port had only the first half: StartAutoStartSessions brought the
// connector up and nothing registered it with the hub, so every acquire on the
// configured session answered 409 "No worker connected for this session." —
// the right refusal for the state the server was in, and the wrong state.

// SetHubLink wires the registry to the hub every started session attaches to.
// ctx bounds the lifetime of the worker bridges: cancelling it (server
// shutdown) stops them reconnecting.
//
// managerURL is the base URL the worker dials back on — the server's own
// public_base_url, because a hosted session's worker socket is a client of the
// same server that hosts it. workerToken is the hub's worker bearer token, sent
// on the handshake when the deployment configures one.
//
// Safe to call once, at server boot, before anything is started.
func (r *SessionRegistryImpl) SetHubLink(ctx context.Context, h *hub.TermHub, managerURL, workerToken string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.bridgeCtx = ctx
	r.hub = h
	r.managerURL = managerURL
	r.workerToken = workerToken
}

// newWorkerBridge builds the worker-side bridge for a started session, or nil
// when no hub link is wired (the registry is usable standalone, and tests that
// never registered a hub get the old connector-only behaviour). Caller holds
// r.mu.
func (r *SessionRegistryImpl) newWorkerBridge(e *sessionEntry) *bridge.TermBridge {
	if r.hub == nil || r.managerURL == "" || r.bridgeCtx == nil || e.conn == nil {
		return nil
	}
	return bridge.New(bridge.Config{
		Worker:      &sessionWorker{conn: e.conn},
		WorkerID:    e.def.SessionID,
		ManagerURL:  r.managerURL,
		InputMode:   e.inputMode,
		BearerToken: r.workerToken,
		// The Go emulator decodes CP437 itself, so the bridge carries raw
		// bytes through byte-faithfully rather than decoding them twice.
		Encoding: "latin-1",
	})
}

// startWorkerBridge attaches a started session to the hub. Caller holds r.mu:
// bridge.Start only records state and spawns the connect goroutine, so it never
// blocks under the lock, and the dial it goes on to perform runs on that
// goroutine rather than on the caller's.
//
// The bridge's lifetime context is the one SetHubLink was given, never the
// caller's: a request context is cancelled when the request that started the
// session returns, which would tear the worker down moments after it attached.
// A registry with no hub link never reaches that context, because there is no
// bridge to give it to.
func (r *SessionRegistryImpl) startWorkerBridge(e *sessionEntry) {
	if e.bridge != nil {
		return
	}
	br := r.newWorkerBridge(e)
	if br == nil {
		return
	}
	e.bridge = br
	br.Start(r.bridgeCtx)
}

// takeBridge detaches and returns a session's worker bridge, for the caller to
// Stop once r.mu is released — Stop waits for the bridge goroutine, which can
// be mid-dial against this same server. Caller holds r.mu.
func takeBridge(e *sessionEntry) *bridge.TermBridge {
	br := e.bridge
	e.bridge = nil
	return br
}

// stopBridge stops a detached worker bridge. Must be called with r.mu released.
func stopBridge(br *bridge.TermBridge) {
	if br != nil {
		br.Stop()
	}
}

// syncHubInputMode mirrors a session's input-mode change onto the hub, which is
// where the REST acquire path reads it from. Port of the reference's
// registry.set_mode, which updates the hub synchronously "so REST acquire
// checks see the new mode immediately" rather than waiting for the worker
// socket to carry it — and which force-releases any live lease on the way back
// to open, so a session everybody may type into is not still held by somebody.
func (r *SessionRegistryImpl) syncHubInputMode(ctx context.Context, id, mode string) {
	r.mu.Lock()
	h := r.hub
	r.mu.Unlock()
	if h == nil {
		return
	}
	if mode == "open" {
		_, _ = h.ForceReleaseHijack(ctx, id)
	}
	_, _, _ = h.SetInputMode(ctx, id, mode)
}

// WorkersAttached reports whether every auto_start session has both come up and
// attached itself to the hub — the sense in which a server is ready to be asked
// for a lease. A registry with no hub link reports on the connectors alone.
func (r *SessionRegistryImpl) WorkersAttached() bool {
	r.mu.Lock()
	h := r.hub
	ids := make([]string, 0, len(r.order))
	for _, id := range r.order {
		e := r.entries[id]
		if !e.def.AutoStart {
			continue
		}
		if e.lifecycle != server.LifecycleRunning {
			r.mu.Unlock()
			return false
		}
		ids = append(ids, id)
	}
	r.mu.Unlock()
	if h == nil {
		return true
	}
	for _, id := range ids {
		if !h.HasWorkerSocket(id) {
			return false
		}
	}
	return true
}
