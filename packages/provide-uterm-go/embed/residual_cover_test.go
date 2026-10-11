//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package embed

import (
	"context"
	"testing"
)

// TestAttachClientFiresLifecycleHook proves AttachClient reports the attach as
// a lifecycle event carrying the client id.
func TestAttachClientFiresLifecycleHook(t *testing.T) {
	s, err := NewHub().CreateSession(Options{SessionID: "life"})
	if err != nil {
		t.Fatal(err)
	}
	var phases []SessionLifecycle
	var details []string
	s.OnLifecycle(func(p SessionLifecycle, d string) {
		phases = append(phases, p)
		details = append(details, d)
	})
	if _, err := s.AttachClient(ClientMetadata{ClientID: "c1"}); err != nil {
		t.Fatal(err)
	}
	if len(phases) != 1 || phases[0] != LifecycleClientAttached || details[0] != "c1" {
		t.Fatalf("lifecycle events = %v %v", phases, details)
	}
}

// TestDetachUnknownClientIsNoop proves detaching an id that was never attached
// leaves the registered clients alone.
func TestDetachUnknownClientIsNoop(t *testing.T) {
	s, err := NewHub().CreateSession(Options{SessionID: "detach"})
	if err != nil {
		t.Fatal(err)
	}
	c, err := s.AttachClient(ClientMetadata{ClientID: "c1"})
	if err != nil {
		t.Fatal(err)
	}
	s.DetachClient("nope")
	if !c.IsAttached() {
		t.Fatal("detaching an unknown id must not affect other clients")
	}
}

// TestSendToClientsReportsApplicationData proves SendToClients both delivers to
// clients and reports the bytes to the application-data hook.
func TestSendToClientsReportsApplicationData(t *testing.T) {
	s, err := NewHub().CreateSession(Options{SessionID: "app"})
	if err != nil {
		t.Fatal(err)
	}
	c, err := s.AttachClient(ClientMetadata{ClientID: "c1"})
	if err != nil {
		t.Fatal(err)
	}
	var gotDir ByteDirection
	var got string
	s.OnApplicationData(func(d ByteDirection, b []byte, _ string) {
		gotDir = d
		got = string(b)
	})
	s.SendToClients([]byte("hi"), nil)
	if gotDir != DirUpstreamToApp || got != "hi" {
		t.Fatalf("app hook got dir=%v data=%q", gotDir, got)
	}
	b, err := c.Receive(context.Background())
	if err != nil || string(b) != "hi" {
		t.Fatalf("client receive = %q, %v", b, err)
	}
}

// TestTryEnqueueRejectsWhenSendWouldBlock covers the final non-blocking send's
// refusal: a slot whose recorded capacity exceeds what its channel can hold
// must report the drop rather than block the fan-out goroutine.
func TestTryEnqueueRejectsWhenSendWouldBlock(t *testing.T) {
	slot := &clientSlot{ch: make(chan []byte), cap: 1}
	if slot.tryEnqueue([]byte("x")) {
		t.Fatal("tryEnqueue must refuse a send that would block")
	}
}

// TestReceiveOnClosedChannelMarksDetached covers Receive observing a closed
// channel whose slot was not yet flagged closed (the close/flag race window).
func TestReceiveOnClosedChannelMarksDetached(t *testing.T) {
	s, err := NewHub().CreateSession(Options{SessionID: "rc"})
	if err != nil {
		t.Fatal(err)
	}
	c, err := s.AttachClient(ClientMetadata{ClientID: "c1"})
	if err != nil {
		t.Fatal(err)
	}
	close(c.slot.ch)
	if _, err := c.Receive(context.Background()); err == nil || err.Error() != "client closed" {
		t.Fatalf("Receive err = %v, want client closed", err)
	}
	if c.IsAttached() {
		t.Fatal("Receive on a closed channel must mark the handle detached")
	}
}

// TestScriptedTelnetReceiveAfterDisconnect proves Receive reports a clean EOF
// (nil, nil) once Disconnect has closed the inbound channel.
func TestScriptedTelnetReceiveAfterDisconnect(t *testing.T) {
	u := NewScriptedTelnetUpstream(nil)
	ctx := context.Background()
	if err := u.Connect(ctx); err != nil {
		t.Fatal(err)
	}
	if err := u.Disconnect(ctx); err != nil {
		t.Fatal(err)
	}
	b, err := u.Receive(ctx)
	if b != nil || err != nil {
		t.Fatalf("Receive after disconnect = %q, %v; want nil, nil", b, err)
	}
}
