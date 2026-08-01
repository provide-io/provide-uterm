//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/frames"
)

// dispatchBrowserMessage handles one inbound browser control/input frame and
// returns the (possibly updated) hijack-ownership flag. Port of the core of
// dispatch_browser_event + handle_browser_message (the interop subset — see the
// deviation note on handleBrowserWS).
func (s *Server) dispatchBrowserMessage(ctx context.Context, conn *websocket.Conn, workerID string, bc *browserConn, role string, canHijack, owned bool, msg map[string]any) bool {
	mtype, _ := msg["type"].(string)
	switch mtype {
	case "input":
		s.browserInput(ctx, workerID, bc, msg)
	case "hijack_request":
		return s.browserHijackRequest(ctx, conn, workerID, bc, role, owned)
	case "hijack_release":
		return s.browserHijackRelease(ctx, workerID, bc)
	case "hijack_step":
		// Match the canonical Python route: stepping is an owner operation,
		// not a capability granted merely by holding a browser connection.
		// TouchIfOwner performs the current/expiry check under the hub lock and
		// renews the dashboard lease before the worker sees the step.
		_, _ = s.deps.Hub.SendBrowserOwnedInput(ctx, workerID, bc,
			controlMsg("step", "dashboard", 0, s.clock.Wall(), ""))
	case "snapshot_req":
		_ = s.deps.Hub.RequestSnapshot(ctx, workerID)
	case "heartbeat":
		s.browserHeartbeat(ctx, conn, workerID, bc)
	case "resume":
		s.browserResume(ctx, conn, workerID, bc, role, msg)
	case "ping":
		s.writeFrame(ctx, bc, frames.PongFrame{Type: frames.TypePong, TS: frames.Ptr(s.clock.Wall())})
	case "presence_update", "queued_input", "control_request":
		s.deckHandle(workerID, bc, msg)
	case "fanout_send":
		s.browserFanoutSend(ctx, bc, msg)
	default:
		// Unhandled types (http_*) are dropped in this interop-subset port.
	}
	return owned
}

// browserResume consumes a single-use resume token and reissues hello with
// resumed=true + a fresh token. A token marked as the disconnected hijack
// owner succeeds only if ownership is still reclaimable; a competitor wins.
func (s *Server) browserResume(ctx context.Context, conn *websocket.Conn, workerID string, bc *browserConn, role string, msg map[string]any) {
	store := s.deps.Hub.ResumeStore()
	if store == nil {
		return
	}
	oldTok, _ := msg["token"].(string)
	if oldTok == "" {
		return
	}
	opCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if err := s.deps.Hub.WaitResumeTokenReady(opCtx, oldTok, bc); err != nil {
		return
	}
	session, err := store.Get(opCtx, oldTok)
	if err != nil || session == nil || session.WorkerID != workerID {
		return
	}
	consumed, err := store.Consume(opCtx, oldTok)
	if err != nil || consumed == nil {
		return
	}
	// Mint a replacement token by re-registering resume metadata for this socket.
	// RegisterBrowser already holds this connection; Create alone is enough.
	newTok, err := store.Create(opCtx, workerID, consumed.Role, 300)
	if err != nil || newTok == "" {
		return
	}
	if consumed.WasHijackOwner && !s.deps.Hub.TryReclaimHijack(opCtx, workerID, bc) {
		_ = store.Revoke(opCtx, newTok)
		return
	}
	if err := s.deps.Hub.ReplaceBrowserResumeToken(opCtx, bc, newTok); err != nil {
		_ = store.Revoke(opCtx, newTok)
		return
	}
	state := s.deps.Hub.RegisterBrowserStateSnapshot(workerID, bc)
	if state == nil {
		state = map[string]any{}
	}
	state["resume_token"] = newTok
	hello := s.buildHelloFrame(workerID, role, role == "admin", state)
	hello.Resumed = frames.Ptr(true)
	hello.ResumeSupported = frames.Ptr(true)
	hello.ResumeToken = frames.Ptr(newTok)
	s.writeFrame(ctx, bc, hello)
	_ = s.deps.Hub.BroadcastHijackState(ctx, workerID)
}

// browserInput forwards keystrokes to the worker through the input-approval
// pipeline (hold/park/resume + policy gate). See browserInputGated.
func (s *Server) browserInput(ctx context.Context, workerID string, bc *browserConn, msg map[string]any) {
	s.browserInputGated(ctx, workerID, bc, msg)
}

// browserHijackRequest acquires the WS hijack lease (admin only).
func (s *Server) browserHijackRequest(ctx context.Context, conn *websocket.Conn, workerID string, bc *browserConn, role string, owned bool) bool {
	if role != "admin" {
		s.writeFrame(ctx, bc, frames.MakeErrorFrame("Hijack requires admin role."))
		return owned
	}
	ok, reason := s.deps.Hub.AcquireWsHijackAndPause(ctx, workerID, bc)
	if !ok {
		s.writeFrame(ctx, bc, frames.MakeErrorFrame(wsHijackErrorMessage(reason)))
		return owned
	}
	_ = s.deps.Hub.BroadcastHijackState(ctx, workerID)
	s.deps.Hub.Metric("hijack_acquires_total", 1)
	s.deps.Hub.NotifyHijackChanged(workerID, true, frames.Ptr("dashboard"))
	return true
}

// browserHijackRelease releases the WS hijack lease, resuming the worker when no
// other lease keeps it paused.
func (s *Server) browserHijackRelease(ctx context.Context, workerID string, bc *browserConn) bool {
	released, restActive, err := s.deps.Hub.ReleaseWsHijack(ctx, workerID, bc)
	if err != nil {
		return false
	}
	if released && !restActive {
		s.deps.Hub.NotifyHijackChanged(workerID, false, nil)
	}
	_ = s.deps.Hub.BroadcastHijackState(ctx, workerID)
	return false
}

// browserHeartbeat refreshes the lease if this browser owns it and acks.
func (s *Server) browserHeartbeat(ctx context.Context, conn *websocket.Conn, workerID string, bc *browserConn) {
	newExp := s.deps.Hub.TouchIfOwner(workerID, bc)
	if newExp == nil {
		return
	}
	s.writeFrame(ctx, bc, frames.HeartbeatAckFrame{
		Type:           frames.TypeHeartbeatAck,
		LeaseExpiresAt: s.monoToWall(*newExp),
		TS:             frames.Ptr(s.clock.Wall()),
	})
	_ = s.deps.Hub.BroadcastHijackState(ctx, workerID)
}

// writeFrame encodes v as a control frame and writes it, ignoring errors (a
// failed write means the socket is gone; the recv loop will observe it).
func (s *Server) writeFrame(ctx context.Context, bc *browserConn, v any) {
	payload, err := encodeFrameControl(v)
	if err != nil {
		return
	}
	_ = bc.SendText(ctx, payload)
}

// wsHijackErrorMessage maps a WS-hijack acquire failure reason to its message.
func wsHijackErrorMessage(reason string) string {
	switch reason {
	case "no_worker":
		return "No worker connected for this session."
	case "open_mode":
		return "Hijack not available in open input mode."
	default:
		return "Already hijacked by another client."
	}
}
