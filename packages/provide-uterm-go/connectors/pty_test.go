//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"bytes"
	"context"
	"errors"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/transports"
)

// TestPTYTransportEcho spawns `cat`, writes a line, and reads it back.
func TestPTYTransportEcho(t *testing.T) {
	ctx := context.Background()
	tr := NewPTYTransport([]string{"cat"})
	if tr.IsConnected() {
		t.Fatal("not connected before Connect")
	}
	// Send/Receive before Connect → ErrNotConnected.
	if err := tr.Send(ctx, []byte("x")); !errors.Is(err, transports.ErrNotConnected) {
		t.Fatalf("Send before connect: %v", err)
	}
	if _, err := tr.Receive(ctx, 4096, 10*time.Millisecond); !errors.Is(err, transports.ErrNotConnected) {
		t.Fatalf("Receive before connect: %v", err)
	}

	if err := tr.Connect(ctx, "", 0, transports.ConnectOptions{Cols: 100, Rows: 40}); err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer func() { _ = tr.Disconnect(ctx) }()
	if !tr.IsConnected() {
		t.Fatal("connected after Connect")
	}

	if err := tr.Send(ctx, []byte("ping-pty\n")); err != nil {
		t.Fatalf("Send: %v", err)
	}

	// cat echoes the line back (PTY also echoes input); read until we see it.
	var got bytes.Buffer
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) && !bytes.Contains(got.Bytes(), []byte("ping-pty")) {
		data, err := tr.Receive(ctx, 4096, 200*time.Millisecond)
		if err != nil {
			t.Fatalf("Receive: %v", err)
		}
		got.Write(data)
	}
	if !bytes.Contains(got.Bytes(), []byte("ping-pty")) {
		t.Fatalf("never read echoed input, got %q", got.String())
	}
}

// TestPTYTransportRemnant exercises the oversized-message remnant path.
func TestPTYTransportRemnant(t *testing.T) {
	ctx := context.Background()
	tr := NewPTYTransport([]string{"cat"})
	if err := tr.Connect(ctx, "", 0, transports.ConnectOptions{}); err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer func() { _ = tr.Disconnect(ctx) }()

	if err := tr.Send(ctx, []byte("abcdefghij\n")); err != nil {
		t.Fatalf("Send: %v", err)
	}
	// Read one byte at a time so the transport stashes/serves a remnant.
	var got bytes.Buffer
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) && !bytes.Contains(got.Bytes(), []byte("abcdefghij")) {
		data, err := tr.Receive(ctx, 1, 200*time.Millisecond)
		if err != nil {
			t.Fatalf("Receive: %v", err)
		}
		if len(data) > 1 {
			t.Fatalf("Receive(1) returned %d bytes", len(data))
		}
		got.Write(data)
	}
	if !bytes.Contains(got.Bytes(), []byte("abcdefghij")) {
		t.Fatalf("remnant read failed, got %q", got.String())
	}
}

// TestPTYTransportChildExitClosesConn asserts Receive reports ErrConnectionClosed
// once a short-lived child exits and its output drains.
func TestPTYTransportChildExitClosesConn(t *testing.T) {
	ctx := context.Background()
	tr := NewPTYTransport([]string{"true"}) // exits immediately
	if err := tr.Connect(ctx, "", 0, transports.ConnectOptions{}); err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer func() { _ = tr.Disconnect(ctx) }()

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		_, err := tr.Receive(ctx, 4096, 200*time.Millisecond)
		if errors.Is(err, transports.ErrConnectionClosed) {
			return // expected
		}
		if err != nil {
			t.Fatalf("unexpected Receive error: %v", err)
		}
	}
	t.Fatal("child exit never surfaced as ErrConnectionClosed")
}

// TestPTYTransportDisconnectIdempotent covers the idempotent Disconnect path.
func TestPTYTransportDisconnectIdempotent(t *testing.T) {
	ctx := context.Background()
	tr := NewPTYTransport(nil)
	// Disconnect before Connect is a no-op.
	if err := tr.Disconnect(ctx); err != nil {
		t.Fatalf("Disconnect before connect: %v", err)
	}
	if err := tr.Connect(ctx, "", 0, transports.ConnectOptions{}); err != nil {
		t.Fatalf("Connect: %v", err)
	}
	if err := tr.Disconnect(ctx); err != nil {
		t.Fatalf("Disconnect: %v", err)
	}
	if err := tr.Disconnect(ctx); err != nil {
		t.Fatalf("Disconnect idempotent: %v", err)
	}
	if tr.IsConnected() {
		t.Fatal("should be disconnected")
	}
}
