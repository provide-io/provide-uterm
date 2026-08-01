//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"crypto/subtle"
	"net/http"
	"strings"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/bridge"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/frames"
)

// registerWSRoutes wires the worker + browser + tunnel WebSocket endpoints.
func (s *Server) registerWSRoutes(mux *http.ServeMux) {
	mux.HandleFunc("/ws/worker/{worker_id}/term", s.handleWorkerWS)
	mux.HandleFunc("/ws/browser/{worker_id}/term", s.handleBrowserWS)
	s.registerTunnelWS(mux)
}

// bearerToken extracts the token from an "Authorization: Bearer <t>" header.
func bearerToken(r *http.Request) string {
	auth := r.Header.Get("Authorization")
	if strings.HasPrefix(auth, "Bearer ") {
		return strings.TrimSpace(strings.TrimPrefix(auth, "Bearer "))
	}
	return ""
}

// handleWorkerWS serves /ws/worker/{id}/term — the endpoint bridge.TermBridge
// dials. Port of ws_worker_term.
func (s *Server) handleWorkerWS(w http.ResponseWriter, r *http.Request) {
	workerID := r.PathValue("worker_id")
	if !validID(workerID) {
		http.NotFound(w, r)
		return
	}
	conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
	if err != nil {
		return
	}
	// Worker-token auth: accept THEN close 1008 so the code is transmitted
	// (matching the Python accept-before-close ordering).
	if tok := s.deps.Hub.WorkerToken(); tok != nil {
		provided := bearerToken(r)
		if subtle.ConstantTimeCompare([]byte(provided), []byte(*tok)) != 1 {
			_ = conn.Close(websocket.StatusPolicyViolation, "authentication required")
			return
		}
	}
	conn.SetReadLimit(int64(s.deps.Hub.MaxWSMessageBytes()))
	wc := &workerConn{wsBase{conn: conn}}

	// A background context outlives the request so deregistration completes even
	// after the socket is torn down.
	bg := context.Background()
	prevHijacked, err := s.deps.Hub.RegisterWorker(bg, workerID, wc)
	if err != nil {
		_ = conn.Close(websocket.StatusPolicyViolation, "worker registration rejected")
		return
	}
	s.deps.Hub.State.TouchActivity(workerID)
	if prevHijacked {
		s.deps.Hub.NotifyHijackChanged(workerID, false, nil)
		_ = s.deps.Hub.BroadcastHijackState(bg, workerID)
	}
	_ = s.deps.Hub.Broadcast(bg, workerID, workerPresenceFrame("worker_connected", workerID, s.clock.Wall()))
	_ = s.deps.Hub.RequestSnapshot(bg, workerID)

	cleanupCtx, cancelCleanup := context.WithCancel(bg)
	go s.periodicHijackCleanup(cleanupCtx, workerID)

	s.workerRecvLoop(r.Context(), conn, workerID, wc)

	cancelCleanup()
	shouldBroadcast, wasHijacked := s.deps.Hub.DeregisterWorker(bg, workerID, wc)
	if shouldBroadcast {
		s.deps.Hub.Metric("ws_disconnect_total", 1)
		s.deps.Hub.Metric("ws_disconnect_worker_total", 1)
		_ = s.deps.Hub.Broadcast(bg, workerID, workerPresenceFrame("worker_disconnected", workerID, s.clock.Wall()))
		if wasHijacked {
			s.deps.Hub.NotifyHijackChanged(workerID, false, nil)
			_ = s.deps.Hub.BroadcastHijackState(bg, workerID)
		}
	}
	_ = s.deps.Hub.PruneIfIdle(bg, workerID)
}

// workerRecvLoop reads and dispatches inbound worker frames until the socket
// closes. Port of the ws_worker_term recv loop.
func (s *Server) workerRecvLoop(ctx context.Context, conn *websocket.Conn, workerID string, wc *workerConn) {
	dec := controlchannel.NewDecoder(controlchannel.DecoderOptions{MaxControlPayloadBytes: s.deps.Hub.MaxWSMessageBytes()})
	bg := context.Background()
	for {
		msgType, raw, err := conn.Read(ctx)
		if err != nil {
			return
		}
		if !s.deps.Hub.IsActiveWorker(bg, workerID, wc) {
			_ = conn.Close(websocket.StatusNormalClosure, "")
			return
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
			return
		}
		for _, ev := range events {
			switch e := ev.(type) {
			case controlchannel.DataChunk:
				if e.Data != "" {
					s.deps.Hub.State.TouchActivity(workerID)
					_ = s.deps.Hub.Broadcast(bg, workerID, map[string]any{"type": "term", "data": e.Data, "ts": s.clock.Wall()})
					_, _ = s.deps.Hub.AppendEventData(bg, workerID, "term", map[string]any{"data": e.Data})
				}
			case controlchannel.ControlChunk:
				if s.dispatchWorkerControl(bg, wc, workerID, e.Control) {
					return
				}
			}
		}
	}
}

// dispatchWorkerControl handles one inbound worker control frame. It returns
// true when the caller should stop the recv loop (protocol mismatch).
func (s *Server) dispatchWorkerControl(ctx context.Context, wc *workerConn, workerID string, msg map[string]any) bool {
	mtype, _ := msg["type"].(string)
	switch mtype {
	case "worker_hello":
		return s.handleWorkerHello(ctx, wc, workerID, msg)
	case "snapshot":
		s.deps.Hub.UpdateLastSnapshot(ctx, workerID, msg)
		_, _ = s.deps.Hub.AppendEventData(ctx, workerID, "snapshot", map[string]any{
			"prompt_id":   extractPromptID(msg),
			"screen_hash": msg["screen_hash"],
			"screen":      msg["screen"],
		})
		_ = s.deps.Hub.Broadcast(ctx, workerID, msg)
	case "analysis":
		_ = s.deps.Hub.Broadcast(ctx, workerID, msg)
	case "status":
		_ = s.deps.Hub.Broadcast(ctx, workerID, msg)
		_, _ = s.deps.Hub.AppendEventData(ctx, workerID, "worker_status", map[string]any{"status": msg})
	default:
		// Other control types are ignored (Python logs + drops them).
	}
	return false
}

// handleWorkerHello negotiates protocol + applies input mode. Returns true when
// the recv loop should stop (protocol mismatch → 1002 close). Port of
// _handle_worker_hello.
func (s *Server) handleWorkerHello(ctx context.Context, wc *workerConn, workerID string, msg map[string]any) bool {
	selected, err := bridge.NegotiateFromHello(msg)
	if err != nil {
		clientMin, clientMax := bridge.ParseClientRange(msg)
		ef := frames.ErrorFrame{
			Type:      frames.TypeError,
			Message:   "protocol_mismatch",
			Reason:    frames.Ptr("protocol_mismatch"),
			ClientMin: frames.Ptr(clientMin),
			ClientMax: frames.Ptr(clientMax),
			ServerMin: frames.Ptr(bridge.MinProtocolVersion),
			ServerMax: frames.Ptr(bridge.MaxProtocolVersion),
		}
		if payload, encErr := encodeFrameControl(ef); encErr == nil {
			_ = wc.SendText(ctx, payload)
		}
		_ = wc.conn.Close(websocket.StatusProtocolError, "protocol_mismatch")
		return true
	}
	mode, _ := msg["input_mode"].(string)
	if mode == "hijack" || mode == "open" {
		if applied, _ := s.deps.Hub.SetWorkerHello(ctx, workerID, mode, &selected); applied {
			_ = s.deps.Hub.BroadcastHijackState(ctx, workerID)
		}
	}
	return false
}

// periodicHijackCleanup runs CleanupExpiredHijack every second while a WS
// handler is active. Port of _periodic_hijack_cleanup.
func (s *Server) periodicHijackCleanup(ctx context.Context, workerID string) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			_, _ = s.deps.Hub.CleanupExpiredHijack(context.Background(), workerID)
		}
	}
}

// workerPresenceFrame builds a worker_connected / worker_disconnected frame map.
func workerPresenceFrame(kind, workerID string, ts float64) map[string]any {
	return map[string]any{"type": kind, "worker_id": workerID, "ts": ts}
}
