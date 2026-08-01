//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"net/http"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnelclient"
)

// registerTunnelWS wires GET/WS /tunnel/{worker_id} — binary tunnel protocol
// (Python tunnel/fastapi_routes.register_tunnel_routes).
func (s *Server) registerTunnelWS(mux *http.ServeMux) {
	mux.HandleFunc("/tunnel/{worker_id}", s.handleTunnelWS)
}

// handleTunnelWS accepts a tunnel agent, registers it as the worker, and fans
// CHANNEL_HTTP JSON frames out to browsers as control frames (inspect e2e).
func (s *Server) handleTunnelWS(w http.ResponseWriter, r *http.Request) {
	workerID := r.PathValue("worker_id")
	if !validID(workerID) {
		http.NotFound(w, r)
		return
	}
	conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
	if err != nil {
		return
	}
	// Worker-token auth (same ordering as handleWorkerWS). In TEST_MODE allow
	// missing token when hub has no worker token configured.
	if tok := s.deps.Hub.WorkerToken(); tok != nil {
		provided := bearerToken(r)
		if subtle.ConstantTimeCompare([]byte(provided), []byte(*tok)) != 1 {
			// Accept-then-close so code 1008 is delivered.
			_ = conn.Close(websocket.StatusPolicyViolation, "authentication required")
			return
		}
	}
	conn.SetReadLimit(int64(s.deps.Hub.MaxWSMessageBytes()))
	wc := &tunnelWorkerConn{wsBase: wsBase{conn: conn}}

	bg := context.Background()
	prevHijacked, err := s.deps.Hub.RegisterWorkerWithTransport(bg, workerID, wc, true)
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

	cleanupCtx, cancelCleanup := context.WithCancel(bg)
	go s.periodicHijackCleanup(cleanupCtx, workerID)

	s.tunnelRecvLoop(r.Context(), conn, workerID, wc)

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

// tunnelRecvLoop reads binary tunnel frames until the socket closes.
func (s *Server) tunnelRecvLoop(ctx context.Context, conn *websocket.Conn, workerID string, wc *tunnelWorkerConn) {
	bg := context.Background()
	max := s.deps.Hub.MaxWSMessageBytes()
	for {
		msgType, raw, err := conn.Read(ctx)
		if err != nil {
			return
		}
		if !s.deps.Hub.IsActiveWorker(bg, workerID, wc) {
			_ = conn.Close(websocket.StatusNormalClosure, "")
			return
		}
		if msgType != websocket.MessageBinary && msgType != websocket.MessageText {
			continue
		}
		if len(raw) > max || len(raw) < 2 {
			continue
		}
		frame, err := tunnelclient.DecodeFrame(raw)
		if err != nil {
			continue
		}
		if frame.IsEOF() {
			continue
		}
		if frame.IsControl() {
			s.handleTunnelControl(bg, workerID, frame.Payload)
			continue
		}
		if frame.Channel == tunnelclient.ChannelHTTP {
			var httpMsg map[string]any
			if err := json.Unmarshal(frame.Payload, &httpMsg); err != nil {
				continue
			}
			httpMsg["_channel"] = "http"
			_ = s.deps.Hub.Broadcast(bg, workerID, httpMsg)
			evType, _ := httpMsg["type"].(string)
			if evType == "" {
				evType = "http"
			}
			_, _ = s.deps.Hub.AppendEventData(bg, workerID, evType, httpMsg)
			s.deps.Hub.State.TouchActivity(workerID)
			continue
		}
		if frame.Channel >= tunnelclient.ChannelData && len(frame.Payload) > 0 {
			text := string(frame.Payload)
			s.deps.Hub.State.TouchActivity(workerID)
			_ = s.deps.Hub.Broadcast(bg, workerID, map[string]any{
				"type": "term", "data": text, "ts": s.clock.Wall(),
			})
			_, _ = s.deps.Hub.AppendEventData(bg, workerID, "term", map[string]any{"data": text})
		}
	}
}

func (s *Server) handleTunnelControl(ctx context.Context, workerID string, payload []byte) {
	msg, err := tunnelclient.DecodeControl(payload)
	if err != nil {
		return
	}
	msgType, _ := msg["type"].(string)
	switch msgType {
	case "open":
		mode, _ := msg["input_mode"].(string)
		if mode == "hijack" || mode == "open" {
			_, _ = s.deps.Hub.SetWorkerHello(ctx, workerID, mode, nil)
		}
	case "snapshot":
		screen, _ := msg["screen"].(string)
		snap := map[string]any{"type": "snapshot", "screen": screen, "ts": s.clock.Wall()}
		s.deps.Hub.UpdateLastSnapshot(ctx, workerID, snap)
		_ = s.deps.Hub.Broadcast(ctx, workerID, snap)
	}
}

// tunnelWorkerConn is a tunnel-protocol worker socket (hub.WorkerWS + TunnelSender).
type tunnelWorkerConn struct {
	wsBase
}

// SendInput sends raw terminal bytes as a tunnel data frame (binary).
func (c *tunnelWorkerConn) SendInput(ctx context.Context, data string) error {
	frame := tunnelclient.EncodeFrame(tunnelclient.ChannelData, []byte(data), tunnelclient.FlagData)
	return c.withWrite(ctx, func() error { return c.conn.Write(ctx, websocket.MessageBinary, frame) })
}

// SendHTTPControl sends an inspect control message as a tunnel HTTP frame.
func (c *tunnelWorkerConn) SendHTTPControl(ctx context.Context, msg map[string]any) error {
	payload, err := json.Marshal(msg)
	if err != nil {
		return err
	}
	frame := tunnelclient.EncodeFrame(tunnelclient.ChannelHTTP, payload, tunnelclient.FlagData)
	return c.withWrite(ctx, func() error { return c.conn.Write(ctx, websocket.MessageBinary, frame) })
}

// Close implements hub.WorkerCloser.
func (c *tunnelWorkerConn) Close(_ context.Context) error {
	return c.conn.Close(websocket.StatusNormalClosure, "")
}

// Ensure tunnelWorkerConn satisfies hub.TunnelSender at compile time.
var (
	_ hub.WorkerWS     = (*tunnelWorkerConn)(nil)
	_ hub.TunnelSender = (*tunnelWorkerConn)(nil)
)
