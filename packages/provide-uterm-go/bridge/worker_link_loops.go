//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package bridge

import (
	"context"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

// enqueueHello queues the worker_hello handshake frame. Port of the
// worker_hello builder (shell.terminal._output.worker_hello) + protocol range.
func (b *TermBridge) enqueueHello() {
	protocol := map[string]any{
		"min":       MinProtocolVersion,
		"max":       MaxProtocolVersion,
		"preferred": PreferredProtocolVersion,
	}
	payload := map[string]any{
		"type":       "worker_hello",
		"input_mode": b.inputMode,
		"ts":         nowTS(),
		"protocol":   protocol,
	}
	if len(b.capabilities) > 0 {
		payload["capabilities"] = b.capabilities
	}
	b.enqueue(queuedFrame{control: payload})
}

// heartbeatLoop emits an inline heartbeat frame at the configured cadence.
func (b *TermBridge) heartbeatLoop(ctx context.Context, cancel context.CancelFunc) {
	defer cancel()
	ticker := time.NewTicker(b.heartbeatInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			b.enqueue(queuedFrame{control: map[string]any{"type": "heartbeat", "ts": nowTS()}})
		}
	}
}

// sendLoop drains the send queue onto the socket. Port of _send_loop.
func (b *TermBridge) sendLoop(ctx context.Context, cancel context.CancelFunc, conn *websocket.Conn) {
	defer cancel()
	for {
		select {
		case <-ctx.Done():
			return
		case f := <-b.sendQ:
			payload, err := encodeQueuedFrame(f)
			if err != nil {
				// Serialization failure: skip the bad message rather than
				// tearing down the connection.
				b.logger.Warn("send_loop_serialization_error", "worker_id", b.workerID, "error", err.Error())
				continue
			}
			if err := conn.Write(ctx, websocket.MessageText, []byte(payload)); err != nil {
				b.logger.Warn("send_loop_network_error", "worker_id", b.workerID, "error", err.Error())
				return
			}
		}
	}
}

// encodeQueuedFrame encodes one queued frame. Port of _encode_bridge_frame.
func encodeQueuedFrame(f queuedFrame) (string, error) {
	if f.isTerm {
		return controlchannel.EncodeTerminalData(f.data), nil
	}
	return controlchannel.EncodeControlFrame(f.control)
}

// recvLoop reads and dispatches inbound frames. On exit it self-clears the
// hijack so the worker is never left paused after a drop. Port of _recv_loop.
func (b *TermBridge) recvLoop(ctx context.Context, cancel context.CancelFunc, conn *websocket.Conn) {
	defer cancel()
	// Self-clear hijack with a fresh context so a cancelled connCtx does not
	// skip the resume. Port of the _recv_loop finally block.
	defer b.setHijacked(context.Background(), false)

	decoder := controlchannel.NewDecoder(controlchannel.DecoderOptions{MaxControlPayloadBytes: b.maxWSMessageBytes})
	for {
		msgType, raw, err := conn.Read(ctx)
		if err != nil {
			b.logger.Debug("recv_loop_read_error", "worker_id", b.workerID, "error", err.Error())
			return
		}
		var chunk string
		if msgType == websocket.MessageBinary {
			chunk = controlchannel.WSBytesToChannelStr(raw)
		} else {
			chunk = string(raw)
		}
		events, err := decoder.Feed(chunk)
		if err != nil {
			b.logger.Debug("recv_loop_bad_stream", "worker_id", b.workerID, "error", err.Error())
			return
		}
		for _, event := range events {
			switch e := event.(type) {
			case controlchannel.DataChunk:
				if e.Data != "" {
					b.sendKeys(ctx, e.Data)
				}
			case controlchannel.ControlChunk:
				b.dispatchControl(ctx, e.Control)
			}
		}
	}
}

// dispatchControl handles one decoded control message. Port of
// _dispatch_control_msg (plus the resume-token receipt this port adds).
func (b *TermBridge) dispatchControl(ctx context.Context, msg map[string]any) {
	mtype, _ := msg["type"].(string)
	switch mtype {
	case "snapshot_req":
		b.sendSnapshot()
	case "control":
		switch action, _ := msg["action"].(string); action {
		case "pause":
			b.setHijacked(ctx, true)
		case "resume":
			b.setHijacked(ctx, false)
		case "step":
			b.requestStep(ctx)
		}
	case "resize":
		b.setSize(ctx, safeInt(msg["cols"], 80, 1), safeInt(msg["rows"], 25, 1))
	case "session_token":
		if token, ok := msg["token"].(string); ok && token != "" {
			b.mu.Lock()
			b.resumeToken = token
			b.mu.Unlock()
			b.logger.Debug("term_bridge_resume_token", "worker_id", b.workerID)
		}
	case "resume_ok", "resume_failed":
		b.logger.Debug("term_bridge_resume_result", "worker_id", b.workerID, "type", mtype)
	default:
		b.mu.Lock()
		handler := b.customHandlers[mtype]
		b.mu.Unlock()
		if handler != nil {
			if err := handler(ctx, msg); err != nil {
				b.logger.Warn("term_bridge_custom_handler_error", "worker_id", b.workerID, "mtype", mtype, "error", err.Error())
			}
		}
	}
}

// sendSnapshot queues a snapshot frame from the live emulator or the cached
// latest snapshot. Port of _send_snapshot (routed through the send queue).
func (b *TermBridge) sendSnapshot() {
	session := b.worker.Session()
	if session == nil {
		return
	}
	b.AttachSession()
	snap := session.Snapshot()
	if snap == nil {
		b.mu.Lock()
		snap = b.latestSnapshot
		b.mu.Unlock()
	}
	if snap == nil {
		snap = map[string]any{}
	}
	b.enqueue(queuedFrame{control: map[string]any{
		"type":               "snapshot",
		"screen":             snapString(snap, "screen", ""),
		"cursor":             snapCursor(snap),
		"cols":               snapInt(snap, "cols", 80),
		"rows":               snapInt(snap, "rows", 25),
		"screen_hash":        snapString(snap, "screen_hash", ""),
		"cursor_at_end":      snapBool(snap, "cursor_at_end", true),
		"has_trailing_space": snapBool(snap, "has_trailing_space", false),
		"prompt_detected":    snap["prompt_detected"],
		"ts":                 nowTS(),
	}})
}

// sendKeys forwards keystrokes to the session, swallowing errors. Port of
// _send_keys.
func (b *TermBridge) sendKeys(ctx context.Context, data string) {
	session := b.worker.Session()
	if session == nil {
		return
	}
	if err := session.Send(ctx, data); err != nil {
		b.logger.Debug("send_keys_failed", "worker_id", b.workerID, "error", err.Error())
	}
}

// requestStep asks the worker to allow one loop iteration, swallowing errors.
// Port of _request_step.
func (b *TermBridge) requestStep(ctx context.Context) {
	if err := b.worker.RequestStep(ctx); err != nil {
		b.logger.Debug("request_step_failed", "worker_id", b.workerID, "error", err.Error())
	}
}

// setSize resizes the session, swallowing errors. Port of _set_size.
func (b *TermBridge) setSize(ctx context.Context, cols, rows int) {
	session := b.worker.Session()
	if session == nil {
		return
	}
	if err := session.SetSize(ctx, cols, rows); err != nil {
		b.logger.Debug("set_size_failed", "worker_id", b.workerID, "error", err.Error())
	}
}

// setHijacked pauses/resumes the worker and queues a status frame — the status
// is queued even when the worker call fails. Port of _set_hijacked.
func (b *TermBridge) setHijacked(ctx context.Context, enabled bool) {
	if err := b.worker.SetHijacked(ctx, enabled); err != nil {
		b.logger.Debug("set_hijacked_failed", "worker_id", b.workerID, "error", err.Error())
	}
	b.enqueue(queuedFrame{control: map[string]any{"type": "status", "hijacked": enabled, "ts": nowTS()}})
}
