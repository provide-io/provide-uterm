//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package tunnelclient

import (
	"context"
	"testing"
	"time"

	"github.com/coder/websocket"
)

// TestClientRecvRawAndMessage covers the success paths of RecvRaw and
// RecvMessage: the server pushes one binary and one text frame.
func TestClientRecvRawAndMessage(t *testing.T) {
	url := wsServer(t, func(ctx context.Context, c *websocket.Conn, _ string) {
		_ = c.Write(ctx, websocket.MessageBinary, []byte("raw-bytes"))
		_ = c.Write(ctx, websocket.MessageText, []byte("text-bytes"))
		time.Sleep(50 * time.Millisecond)
	})

	c := NewClient(url, "")
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := c.Connect(ctx); err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer func() { _ = c.Close() }()

	raw, err := c.RecvRaw(ctx)
	if err != nil || string(raw) != "raw-bytes" {
		t.Fatalf("RecvRaw = %q, err %v", raw, err)
	}

	isText, data, err := c.RecvMessage(ctx)
	if err != nil {
		t.Fatalf("RecvMessage err: %v", err)
	}
	if !isText || string(data) != "text-bytes" {
		t.Fatalf("RecvMessage isText=%v data=%q", isText, data)
	}
}
