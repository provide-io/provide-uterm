//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/deckmux"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
)

// deckBroadcaster adapts the hub's browser broadcast to deckmux.Broadcaster so
// presence/control_transfer frames fan out to every browser of a session —
// the Go analogue of the Python DeckMuxPresence(hub) back-reference calling
// hub.broadcast(worker_id, msg).
type deckBroadcaster struct{ h *hub.TermHub }

// Broadcast forwards a DeckMux message to all browsers of workerID via the hub.
func (b deckBroadcaster) Broadcast(workerID string, msg map[string]any) error {
	return b.h.Broadcast(context.Background(), workerID, msg)
}

// deck lazily constructs (once) the per-server DeckMux presence service wired to
// the hub broadcast. Mirrors the app-factory wiring that sets
// hub.deckmux_* attributes on the Python server.
func (s *Server) deck() *deckmux.DeckMuxPresence {
	s.deckOnce.Do(func() {
		s.deckPresence = deckmux.NewDeckMuxPresence(deckBroadcaster{s.deps.Hub})
	})
	return s.deckPresence
}

// deckPrincipalT is the deckmux.Principal (+ optional DisplayNamed) adapter over
// a resolved server principal.
type deckPrincipalT struct{ subject, display string }

// SubjectID implements deckmux.Principal.
func (p deckPrincipalT) SubjectID() string { return p.subject }

// DisplayName implements deckmux.DisplayNamed ("" falls back to the subject id
// inside OnBrowserConnect, matching the Python getattr default).
func (p deckPrincipalT) DisplayName() string { return p.display }

// deckPrincipalFor returns the deckmux principal for a browser connection, or
// nil for an anonymous/unauthenticated browser (the stable per-connection
// anonymous id path). Mirrors the Python ws.state.uterm_principal lookup.
func deckPrincipalFor(bc *browserConn) any {
	p := bc.principal
	if p == nil || p.SubjectID == "" || p.SubjectID == "anonymous" {
		return nil
	}
	display := ""
	if p.DisplayName != nil {
		display = *p.DisplayName
	}
	return deckPrincipalT{subject: p.SubjectID, display: display}
}

// deckOnConnect registers a connecting browser with DeckMux and sends it the
// presence_sync frame. Called from the browser handshake BEFORE broadcasts are
// activated, so OnBrowserConnect's fan-out to existing browsers skips this
// (still startup-pending) socket — matching the Python ordering. The sync is
// written through bc.SendText so it serialises with concurrent hub writes.
func (s *Server) deckOnConnect(ctx context.Context, bc *browserConn, workerID, role string) {
	sync, err := s.deck().OnBrowserConnect(workerID, bc, role, deckPrincipalFor(bc))
	if err != nil {
		s.logger.Debug("deckmux_connect_failed", "worker_id", workerID, "error", err)
	}
	if sync == nil {
		return
	}
	payload, e := encodeControlMap(sync)
	if e != nil {
		return
	}
	_ = bc.SendText(ctx, payload)
}

// deckHandle routes a presence_update / queued_input / control_request browser
// message through DeckMux. Port of the deckmux_handle_message dispatch branch
// in dispatch_browser_event.
func (s *Server) deckHandle(workerID string, bc *browserConn, msg map[string]any) {
	if err := s.deck().HandleMessage(workerID, bc, msg, deckPrincipalFor(bc)); err != nil {
		s.logger.Debug("deckmux_handle_failed", "worker_id", workerID, "error", err)
	}
}

// deckOnDisconnect removes a disconnecting browser from DeckMux (broadcasting a
// presence_leave). Port of the deckmux_on_browser_disconnect finally branch.
func (s *Server) deckOnDisconnect(workerID string, bc *browserConn) {
	if err := s.deck().OnBrowserDisconnect(workerID, bc, deckPrincipalFor(bc)); err != nil {
		s.logger.Debug("deckmux_disconnect_failed", "worker_id", workerID, "error", err)
	}
}
