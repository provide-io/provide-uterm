//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package termsession

// The session keeps the close its transport reported. Port of the session half
// of Python's tests/test_transport_close.py (dc87968c).

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/transports"
)

func goingAway() transports.TransportClose {
	code := 1001
	return transports.TransportClose{Initiator: transports.CloseRemote, Code: &code, Reason: "going away"}
}

func untilDisconnected(t *testing.T, s *TransportSession) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for s.IsConnected() {
		if time.Now().After(deadline) {
			t.Fatal("the reader never noticed the transport close")
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func TestANewSessionHasNoClose(t *testing.T) {
	s := New(&fakeTransport{}, func(context.Context) error { return nil }, Options{})
	if s.CloseInfo() != nil {
		t.Error("a new session has no close")
	}
}

func TestTheReaderKeepsTheTransportClose(t *testing.T) {
	ft := &fakeTransport{recvErr: &transports.TransportClosedError{Message: "connection closed", Close: goingAway()}}
	s := newFakeSession(t, ft, Options{})
	untilDisconnected(t, s)

	got := s.CloseInfo()
	if got == nil || got.Summary() != "remote close 1001 going away" {
		t.Fatalf("CloseInfo = %+v", got)
	}
	// It is a copy.
	got.Reason = "mutated"
	if s.CloseInfo().Reason != "going away" {
		t.Error("CloseInfo must return a copy")
	}
}

func TestAnUntypedDropIsAnUnknownCloseWithItsDetail(t *testing.T) {
	ft := &fakeTransport{recvErr: errors.New("peer reset")}
	s := newFakeSession(t, ft, Options{})
	untilDisconnected(t, s)

	got := s.CloseInfo()
	if got == nil || got.Initiator != transports.CloseUnknown || got.Detail != "*errors.errorString: peer reset" {
		t.Fatalf("CloseInfo = %+v", got)
	}
}

func TestClosingTheSessionIsALocalClose(t *testing.T) {
	s := newFakeSession(t, &fakeTransport{}, Options{})
	if err := s.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	got := s.CloseInfo()
	if got == nil || got.Summary() != "local close (closed by client)" {
		t.Fatalf("CloseInfo = %+v", got)
	}
}

func TestClosingAfterTheTransportClosedKeepsTheTransportClose(t *testing.T) {
	ft := &fakeTransport{recvErr: &transports.TransportClosedError{Message: "connection closed", Close: goingAway()}}
	s := newFakeSession(t, ft, Options{})
	untilDisconnected(t, s)

	if err := s.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := s.CloseInfo(); got == nil || got.Summary() != "remote close 1001 going away" {
		t.Fatalf("CloseInfo = %+v", got)
	}
}

// gatedTransport parks the reader inside Receive until released, then fails.
type gatedTransport struct {
	fakeTransport
	entered chan struct{}
	release chan struct{}
}

func (g *gatedTransport) Receive(context.Context, int, time.Duration) ([]byte, error) {
	g.entered <- struct{}{}
	<-g.release
	return nil, errors.New("use of closed network connection")
}

func TestAReceiveErrorDuringCloseIsNotRecorded(t *testing.T) {
	// The reader is inside Receive when the session starts closing; the error
	// that closing causes is this side's own close, not the transport's.
	g := &gatedTransport{entered: make(chan struct{}), release: make(chan struct{})}
	s := New(g, func(context.Context) error { return nil }, Options{})
	if err := s.Connect(context.Background()); err != nil {
		t.Fatal(err)
	}
	<-g.entered
	s.mu.Lock()
	s.connected = false // what Close does first
	done := s.readerDone
	s.mu.Unlock()
	close(g.release)
	<-done
	if got := s.CloseInfo(); got != nil {
		t.Fatalf("CloseInfo = %+v, want nil", got)
	}
}

func TestConnectResetsTheClose(t *testing.T) {
	ft := &fakeTransport{}
	s := newFakeSession(t, ft, Options{})
	if err := s.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if s.CloseInfo() == nil {
		t.Fatal("expected a local close")
	}
	if err := s.Connect(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := s.CloseInfo(); got != nil {
		t.Errorf("CloseInfo after reconnect = %+v", got)
	}
}
