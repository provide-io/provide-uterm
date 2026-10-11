//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

// TestPTYReadLoopStopsOnQuitWhileBlockedOnDelivery proves the read loop does
// not wedge on a consumer that stopped draining: with nobody receiving on rxCh
// and quit closed, the loop must drop the chunk, exit and close `closed`.
func TestPTYReadLoopStopsOnQuitWhileBlockedOnDelivery(t *testing.T) {
	tr := &PTYTransport{}
	rx := make(chan []byte) // unbuffered and never read
	closed := make(chan struct{})
	quit := make(chan struct{})
	close(quit)

	tr.readLoop(strings.NewReader("output"), rx, closed, quit)

	select {
	case <-closed:
	default:
		t.Fatal("readLoop must close `closed` when it returns")
	}
	select {
	case b := <-rx:
		t.Fatalf("no chunk may be delivered after quit, got %q", b)
	default:
	}
}

// TestPTYReceiveHonoursCancelledContext proves a Receive whose context is
// already cancelled returns ctx.Err() instead of waiting out its timeout, when
// the PTY is connected but silent.
func TestPTYReceiveHonoursCancelledContext(t *testing.T) {
	tr := &PTYTransport{rxCh: make(chan []byte), closed: make(chan struct{})}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	got, err := tr.Receive(ctx, 4096, time.Hour)
	if !errors.Is(err, context.Canceled) || got != nil {
		t.Fatalf("Receive = %q, %v; want nil, context.Canceled", got, err)
	}
}
