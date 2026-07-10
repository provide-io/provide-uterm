//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bytes"
	"context"
	"testing"
	"time"

	"github.com/coder/websocket"
)

// TestBridgeShareMidStreamClose drives bridgeShare through a live tunnel that
// closes the socket mid-stream. It exercises the inbound empty-frame skip, the
// inbound raw-write path, and — crucially — the PTY→WS SendData error branch that
// fires when the outbound write races a real closed connection.
func TestBridgeShareMidStreamClose(t *testing.T) {
	f := newFakeTunnelServer(t, func(ctx context.Context, c *websocket.Conn) {
		// Wait for the first framed PTY output so streaming is under way.
		if _, _, err := c.Read(ctx); err != nil {
			return
		}
		// An empty inbound frame must be skipped; a non-empty one is written to
		// the PTY. Both are flushed before the close frame.
		_ = c.Write(ctx, websocket.MessageBinary, []byte{})
		_ = c.Write(ctx, websocket.MessageBinary, []byte("inbound"))
		// A normal close: subsequent outbound SendData calls from the still-live
		// PTY (a gentle, never-ending producer) then error, unwinding the bridge.
		_ = c.Close(websocket.StatusNormalClosure, "bye")
	})

	var out bytes.Buffer
	done := make(chan error, 1)
	go func() {
		// A low-rate infinite producer keeps PTY output flowing (so an outbound
		// SendData is attempted after the close) without pegging a CPU like `yes`.
		done <- runShare(context.Background(), shareOptions{
			Server: f.srv.URL, Cmd: []string{"sh", "-c", "while true; do printf x; sleep 0.02; done"},
		}, &out)
	}()

	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("runShare returned %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("bridgeShare did not unwind after the server closed mid-stream")
	}
}
