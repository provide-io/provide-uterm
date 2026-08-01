//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"net/http"
	"os"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/bridge"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/frames"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
)

// resolveBrowserRole resolves a dashboard viewer role from the principal and the
// session definition: "" means the caller may not view the session. Port of the
// default-RBAC path of hub.resolve_role_for_browser (authz.can_read_session +
// authz.resolve_browser_role).
func (s *Server) resolveBrowserRole(ctx context.Context, p *serverauth.Principal, workerID string) string {
	def, ok := s.deps.Registry.GetDefinition(ctx, workerID)
	if !ok {
		return ""
	}
	if !s.deps.Authz.CanReadSession(p, def) {
		return ""
	}
	return s.deps.Authz.ResolveBrowserRole(p, def)
}

// handleBrowserWS serves /ws/browser/{id}/term — dashboard viewers + hijack
// control. Port of ws_browser_term.
//
// Deviations (documented): resume-token reclaim is omitted. This handler covers
// viewer streaming + the WS hijack lifecycle (request / release / step /
// heartbeat) + input (with the per-frame token-bucket rate limits, the
// lease/permission gate prepare_browser_input, and the input-approval hold/park
// pipeline, see browserRecvLoop + browserInputGated) + snapshot_req/ping +
// fanout_send (see browserFanoutSend), plus DeckMux collaborative presence
// (connect/message/disconnect).
func (s *Server) handleBrowserWS(w http.ResponseWriter, r *http.Request) {
	workerID := r.PathValue("worker_id")
	if !validID(workerID) {
		http.NotFound(w, r)
		return
	}
	conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
	if err != nil {
		return
	}
	bg := context.Background()
	// UTERM_TEST_MODE=1: multi-backend Playwright e2e — open admin browser for any worker_id.
	// Never default-on; production servers must not set this env.
	if os.Getenv("UTERM_TEST_MODE") == "1" {
		principal := &serverauth.Principal{SubjectID: "test-admin", Roles: serverauth.NewSet("admin")}
		bc := &browserConn{wsBase: wsBase{conn: conn}, principal: principal}
		role, canHijack := "admin", true
		state, err := s.deps.Hub.RegisterBrowser(bg, workerID, bc, role, true)
		if err != nil {
			_ = conn.Close(websocket.StatusPolicyViolation, "browser registration rejected")
			return
		}
		if s.deps.BrowserSetupHook != nil && s.deps.BrowserSetupHook() != nil {
			s.browserCleanup(bg, workerID, bc, false)
			_ = conn.Close(websocket.StatusInternalError, "browser setup failed")
			return
		}
		s.deps.Hub.State.TouchActivity(workerID)
		if !s.browserHandshake(bg, conn, workerID, bc, role, canHijack, state) {
			s.deckOnDisconnect(workerID, bc)
			s.browserCleanup(bg, workerID, bc, false)
			return
		}
		owned := s.browserRecvLoop(r.Context(), conn, workerID, bc, role, canHijack)
		// Same ordering as the production path below: DeckMux leave before hub cleanup.
		s.deckOnDisconnect(workerID, bc)
		s.browserCleanup(bg, workerID, bc, owned)
		return
	}
	principal := s.resolvePrincipal(r)
	if isAnonymous(principal) {
		s.deps.Hub.Metric("auth_failures_ws_total", 1)
		_ = conn.Close(websocket.StatusPolicyViolation, "authentication required")
		return
	}
	role := s.resolveBrowserRole(bg, principal, workerID)
	if role == "" {
		_ = conn.Close(websocket.StatusPolicyViolation, "insufficient privileges")
		return
	}
	bc := &browserConn{wsBase: wsBase{conn: conn}, principal: principal}
	canHijack := role == "admin"

	state, err := s.deps.Hub.RegisterBrowser(bg, workerID, bc, role, true)
	if err != nil {
		_ = conn.Close(websocket.StatusPolicyViolation, "browser registration rejected")
		return
	}
	if s.deps.BrowserSetupHook != nil && s.deps.BrowserSetupHook() != nil {
		s.browserCleanup(bg, workerID, bc, false)
		_ = conn.Close(websocket.StatusInternalError, "browser setup failed")
		return
	}
	s.deps.Hub.State.TouchActivity(workerID)

	if !s.browserHandshake(bg, conn, workerID, bc, role, canHijack, state) {
		s.browserCleanup(bg, workerID, bc, false)
		return
	}

	cleanupCtx, cancel := context.WithCancel(bg)
	go s.periodicHijackCleanup(cleanupCtx, workerID)

	owned := s.browserRecvLoop(r.Context(), conn, workerID, bc, role, canHijack)
	cancel()
	// DeckMux disconnect (broadcasts presence_leave) runs before the hub
	// browser cleanup, mirroring the Python finally ordering.
	s.deckOnDisconnect(workerID, bc)
	s.browserCleanup(bg, workerID, bc, owned)
}

// browserHandshake sends the hello + hijack-state (+ initial snapshot) frames.
// Returns false when a write fails (the socket is gone).
func (s *Server) browserHandshake(ctx context.Context, conn *websocket.Conn, workerID string, bc *browserConn, role string, canHijack bool, state map[string]any) bool {
	hello := s.buildHelloFrame(workerID, role, canHijack, state)
	payload, err := encodeFrameControl(hello)
	if err != nil || bc.SendText(ctx, payload) != nil {
		return false
	}
	hijackState := s.deps.Hub.HijackStateMsgFor(ctx, workerID, bc)
	if p, e := encodeFrameControl(hijackState); e == nil {
		if bc.SendText(ctx, p) != nil {
			return false
		}
	}
	if snap, ok := state["initial_snapshot"].(map[string]any); ok && snap != nil {
		if p, e := encodeControlMap(snap); e == nil {
			_ = bc.SendText(ctx, p)
		}
	} else {
		_ = s.deps.Hub.RequestSnapshot(ctx, workerID)
	}
	// Register with DeckMux + send presence_sync BEFORE activating broadcasts,
	// so the fan-out to existing browsers skips this still-startup-pending
	// socket (matching the Python on_browser_connect ordering).
	s.deckOnConnect(ctx, bc, workerID, role)
	s.deps.Hub.ActivateBrowserBroadcasts(ctx, workerID, bc)
	return true
}

// buildHelloFrame constructs the server→browser hello frame. Port of
// make_hello_frame(**kwargs).
func (s *Server) buildHelloFrame(workerID, role string, canHijack bool, state map[string]any) frames.HelloFrame {
	resumeSupported := s.deps.Hub.ResumeStore() != nil
	inputMode := stringField(state, "input_mode")
	// Capability defaults match spec/behavior.json hello_defaults.go
	// (mcp_supported=true, vnc_supported=true) — same stamp as Python
	// make_hello_frame for mcp; Go advertises VNC because the VNC package ships.
	hf := frames.HelloFrame{
		Type:                frames.TypeHello,
		WorkerID:            frames.Ptr(workerID),
		CanHijack:           frames.Ptr(canHijack),
		Hijacked:            frames.Ptr(boolField(state, "is_hijacked", false)),
		HijackedByMe:        frames.Ptr(boolField(state, "hijacked_by_me", false)),
		WorkerOnline:        frames.Ptr(boolField(state, "worker_online", false)),
		InputMode:           frames.Ptr(inputMode),
		Role:                frames.Ptr(role),
		HijackControl:       frames.Ptr("ws"),
		HijackStepSupported: frames.Ptr(true),
		Capabilities:        map[string]any{"hijack_control": "ws", "hijack_step_supported": true},
		ResumeSupported:     frames.Ptr(resumeSupported),
		McpSupported:        frames.Ptr(true),
		VncSupported:        frames.Ptr(true),
		ProtocolVersion:     frames.Ptr(bridge.CurrentProtocolVersion),
		Protocol: map[string]int{
			"selected":   bridge.PreferredProtocolVersion,
			"server_min": bridge.MinProtocolVersion,
			"server_max": bridge.MaxProtocolVersion,
		},
	}
	if tok := stringField(state, "resume_token"); tok != "" {
		hf.ResumeToken = frames.Ptr(tok)
	}
	return hf
}

// browserRecvLoop reads and dispatches inbound browser frames, returning whether
// this browser ever owned the hijack lease (for cleanup resume logic).
func (s *Server) browserRecvLoop(ctx context.Context, conn *websocket.Conn, workerID string, bc *browserConn, role string, canHijack bool) bool {
	dec := controlchannel.NewDecoder(controlchannel.DecoderOptions{MaxControlPayloadBytes: s.deps.Hub.MaxWSMessageBytes()})
	conn.SetReadLimit(int64(s.deps.Hub.MaxWSMessageBytes()))
	bg := context.Background()
	// Per-connection token buckets rate-limit input + control frames separately.
	buckets := s.newBrowserBuckets()
	owned := false
	for {
		msgType, raw, err := conn.Read(ctx)
		if err != nil {
			return owned
		}
		var chunk string
		if msgType == websocket.MessageBinary {
			chunk = controlchannel.WSBytesToChannelStr(raw)
		} else {
			chunk = string(raw)
		}
		events, ferr := dec.Feed(chunk)
		if ferr != nil {
			_ = conn.Close(websocket.StatusUnsupportedData, ferr.Error())
			return owned
		}
		for _, ev := range events {
			var msg map[string]any
			switch e := ev.(type) {
			case controlchannel.DataChunk:
				msg = map[string]any{"type": "input", "data": e.Data}
			case controlchannel.ControlChunk:
				msg = e.Control
			}
			mtype, _ := msg["type"].(string)
			// Per-frame rate limit: an exceeded frame is dropped (with an error
			// frame + metric) before it reaches the dispatch/DeckMux/approval paths.
			if !s.rateLimitBrowserFrame(bg, bc, workerID, mtype, buckets) {
				continue
			}
			owned = s.dispatchBrowserMessage(bg, conn, workerID, bc, role, canHijack, owned, msg)
		}
	}
}

// browserCleanup runs the disconnect resume/notify sequence. Port of the
// ws_browser_term finally block.
func (s *Server) browserCleanup(ctx context.Context, workerID string, bc *browserConn, owned bool) {
	s.deps.Hub.Metric("ws_disconnect_total", 1)
	s.deps.Hub.Metric("ws_disconnect_browser_total", 1)
	s.deps.Hub.State.TouchActivity(workerID)
	result, err := s.deps.Hub.CleanupBrowserDisconnect(ctx, workerID, bc, owned)
	if err != nil {
		return
	}
	wasOwner := boolField(result, "was_owner", false)
	if wasOwner {
		_ = s.deps.Hub.BroadcastHijackState(ctx, workerID)
		_, _ = s.deps.Hub.AppendEventData(ctx, workerID, "hijack_released",
			map[string]any{"owner": "dashboard_ws_disconnect"})
	}
	_ = s.deps.Hub.PruneIfIdle(ctx, workerID)
}

// boolField reads a bool JSON field with a default.
func boolField(m map[string]any, k string, def bool) bool {
	if v, ok := m[k].(bool); ok {
		return v
	}
	return def
}
