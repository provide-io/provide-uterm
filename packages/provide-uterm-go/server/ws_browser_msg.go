//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"

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
		_, _ = s.deps.Hub.SendWorker(ctx, workerID, controlMsg("step", "dashboard", 0, s.clock.Wall(), ""))
	case "snapshot_req":
		_ = s.deps.Hub.RequestSnapshot(ctx, workerID)
	case "heartbeat":
		s.browserHeartbeat(ctx, conn, workerID, bc)
	case "ping":
		s.writeFrame(ctx, conn, frames.PongFrame{Type: frames.TypePong, TS: frames.Ptr(s.clock.Wall())})
	case "presence_update", "queued_input", "control_request":
		s.deckHandle(workerID, bc, msg)
	default:
		// Unhandled types (fanout, resume, http_*) are dropped in this
		// interop-subset port.
	}
	return owned
}

// browserInput forwards keystrokes to the worker through the input-approval
// pipeline (hold/park/resume + policy gate). See browserInputGated.
func (s *Server) browserInput(ctx context.Context, workerID string, bc *browserConn, msg map[string]any) {
	s.browserInputGated(ctx, workerID, bc, msg)
}

// browserHijackRequest acquires the WS hijack lease (admin only).
func (s *Server) browserHijackRequest(ctx context.Context, conn *websocket.Conn, workerID string, bc *browserConn, role string, owned bool) bool {
	if role != "admin" {
		s.writeFrame(ctx, conn, frames.MakeErrorFrame("Hijack requires admin role."))
		return owned
	}
	_, _ = s.deps.Hub.SendWorker(ctx, workerID, controlMsg("pause", "dashboard", 0, s.clock.Wall(), ""))
	ok, reason := s.deps.Hub.TryAcquireWsHijack(ctx, workerID, bc)
	if !ok {
		if reason != "already_hijacked" {
			_, _ = s.deps.Hub.SendWorker(ctx, workerID, controlMsg("resume", "dashboard", 0, s.clock.Wall(), ""))
		}
		s.writeFrame(ctx, conn, frames.MakeErrorFrame(wsHijackErrorMessage(reason)))
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
	released, restActive := s.deps.Hub.TryReleaseWsHijack(ctx, workerID, bc)
	if released && !restActive && !s.deps.Hub.CheckStillHijacked(workerID) {
		_, _ = s.deps.Hub.SendWorker(ctx, workerID, controlMsg("resume", "dashboard", 0, s.clock.Wall(), ""))
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
	s.writeFrame(ctx, conn, frames.HeartbeatAckFrame{
		Type:           frames.TypeHeartbeatAck,
		LeaseExpiresAt: s.monoToWall(*newExp),
		TS:             frames.Ptr(s.clock.Wall()),
	})
	_ = s.deps.Hub.BroadcastHijackState(ctx, workerID)
}

// writeFrame encodes v as a control frame and writes it, ignoring errors (a
// failed write means the socket is gone; the recv loop will observe it).
func (s *Server) writeFrame(ctx context.Context, conn *websocket.Conn, v any) {
	payload, err := encodeFrameControl(v)
	if err != nil {
		return
	}
	_ = conn.Write(ctx, websocket.MessageText, []byte(payload))
}

// bcConn returns the underlying websocket connection of a browserConn.
func bcConn(bc *browserConn) *websocket.Conn { return bc.conn }

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
