//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

// ReconnectingTransport.LastClose keeps the close that caused the last
// reconnect. Port of Python's tests/transports/test_reconnect_last_close.py.

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestReconnectKeepsTheCloseThatCausedIt(t *testing.T) {
	goingAway := TransportClose{Initiator: CloseRemote, Code: codePtr(1001), Reason: "going away"}
	failing := newStubTransport()
	failing.sendErr = closedError("connection closed", goingAway, nil)
	recovered := newStubTransport()
	factory, _ := listFactory(failing, recovered)
	sleep, _ := recordingSleep()
	rt := NewReconnectingTransport(factory, ReconnectingOptions{Sleep: sleep})
	ctx := context.Background()
	if err := rt.Connect(ctx, "h", 1, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	if rt.LastClose() != nil {
		t.Fatal("no close before any reconnect")
	}
	if err := rt.Send(ctx, []byte("hello")); err != nil {
		t.Fatalf("send: %v", err)
	}
	got := rt.LastClose()
	if got == nil || got.Summary() != "remote close 1001 going away" {
		t.Fatalf("LastClose = %+v", got)
	}
	// The returned close is a copy.
	got.Reason = "mutated"
	if rt.LastClose().Reason != "going away" {
		t.Error("LastClose must return a copy")
	}
}

func TestReconnectOnAnUntypedErrorLeavesNoLastClose(t *testing.T) {
	failing := newStubTransport()
	failing.sendErr = ErrConnectionClosed
	recovered := newStubTransport()
	factory, _ := listFactory(failing, recovered)
	sleep, _ := recordingSleep()
	rt := NewReconnectingTransport(factory, ReconnectingOptions{Sleep: sleep})
	ctx := context.Background()
	if err := rt.Connect(ctx, "h", 1, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	if err := rt.Send(ctx, []byte("hello")); err != nil {
		t.Fatalf("send: %v", err)
	}
	if got := rt.LastClose(); got != nil {
		t.Errorf("LastClose = %+v, want nil", got)
	}
}

func TestReconnectExhaustedStillRecordsTheClose(t *testing.T) {
	lost := TransportClose{Initiator: CloseUnknown, Detail: "reset"}
	s1 := newStubTransport()
	s1.receiveErr = closedError("connection lost", lost, nil)
	factory, _ := listFactory(s1)
	sleep, _ := recordingSleep()
	policy := ReconnectPolicy{MaxRetries: 0}
	rt := NewReconnectingTransport(factory, ReconnectingOptions{Policy: &policy, Sleep: sleep})
	ctx := context.Background()
	if err := rt.Connect(ctx, "h", 1, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	if _, err := rt.Receive(ctx, 16, time.Millisecond); !errors.Is(err, ErrRetriesExhausted) {
		t.Fatalf("err = %v", err)
	}
	if got := rt.LastClose(); got == nil || got.Summary() != "unknown close (reset)" {
		t.Errorf("LastClose = %+v", got)
	}
}
