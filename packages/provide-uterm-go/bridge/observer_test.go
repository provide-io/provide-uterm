//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package bridge

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
)

// recordingObserver logs every Observer call as one line, in order. failOn
// names a method that fails, as a recording whose store has gone away does.
type recordingObserver struct {
	mu     sync.Mutex
	calls  []string
	failOn string
}

var errObserver = errors.New("recording store down")

func (o *recordingObserver) fail(method string) error {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.failOn == method {
		return errObserver
	}
	return nil
}

func (o *recordingObserver) add(format string, args ...any) {
	o.mu.Lock()
	o.calls = append(o.calls, fmt.Sprintf(format, args...))
	o.mu.Unlock()
}

func (o *recordingObserver) AttemptStarted() { o.add("attempt") }
func (o *recordingObserver) Connected() error {
	o.add("connected")
	return o.fail("Connected")
}
func (o *recordingObserver) FrameSent(payload string, frame map[string]any) error {
	if frame["type"] == "term" {
		o.add("sent term %q payload=%t", frame["data"], strings.Contains(payload, frame["data"].(string)))
	} else {
		o.add("sent %v", frame["type"])
	}
	return o.fail("FrameSent")
}
func (o *recordingObserver) WireReceived(text string) error {
	o.add("wire %q", text)
	return o.fail("WireReceived")
}
func (o *recordingObserver) ControlReceived(msg map[string]any) error {
	o.add("control %v", msg["type"])
	return o.fail("ControlReceived")
}
func (o *recordingObserver) InputReceived(data string) error {
	o.add("input %q", data)
	return o.fail("InputReceived")
}
func (o *recordingObserver) AttemptEnded(err error) {
	if err != nil {
		o.add("ended error")
		return
	}
	o.add("ended")
}

func (o *recordingObserver) has(call string) bool {
	o.mu.Lock()
	defer o.mu.Unlock()
	for _, c := range o.calls {
		if c == call {
			return true
		}
	}
	return false
}

func (o *recordingObserver) snapshot() []string {
	o.mu.Lock()
	defer o.mu.Unlock()
	return append([]string(nil), o.calls...)
}

// The observer sees one connection's traffic the way the reference's hosted
// runtime logs it: the attempt opens, every frame actually written is reported
// with its wire payload, inbound text is reported raw and then per decoded
// event, and the attempt closes when the connection does.
func TestObserverSeesOneConnectionsTraffic(t *testing.T) {
	hub := newFakeHub(t)
	session := &mockSession{snapshot: map[string]any{"screen": "HELLO"}}
	obs := &recordingObserver{}
	b := New(Config{
		Worker:     &mockWorker{session: session},
		WorkerID:   "w1",
		ManagerURL: hub.baseURL(),
		Observer:   obs,
	})
	b.Start(context.Background())

	conn := hub.awaitConn(t)
	hub.awaitControl(t, "worker_hello")
	waitFor(t, "hello reported", func() bool { return obs.has("sent worker_hello") })

	hub.writeData(t, conn, "ls\r")
	waitFor(t, "input reported", func() bool { return obs.has(`input "ls\r"`) })
	if !obs.has(`wire "ls\r"`) {
		t.Fatalf("inbound text not reported raw: %v", obs.snapshot())
	}

	hub.writeControl(t, conn, map[string]any{"type": "snapshot_req"})
	waitFor(t, "snapshot reported", func() bool { return obs.has("sent snapshot") })
	if !obs.has("control snapshot_req") {
		t.Fatalf("inbound control not reported: %v", obs.snapshot())
	}

	session.fire(map[string]any{}, []byte("out"))
	waitFor(t, "term reported", func() bool { return obs.has(`sent term "out" payload=true`) })

	b.Stop()
	calls := obs.snapshot()
	if calls[0] != "attempt" || calls[1] != "connected" {
		t.Fatalf("an attempt opens then connects, got %v", calls)
	}
	if calls[len(calls)-1] != "ended" {
		t.Fatalf("a stopped connection ends its attempt without an error, got %v", calls)
	}
	if keys := session.sentKeys(); len(keys) != 1 {
		t.Fatalf("input not delivered: %v", keys)
	}
}

// A dial that fails ends its attempt with the error, which is what lets the
// session record a runtime_error for it.
func TestObserverSeesAFailedDial(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()
	obs := &recordingObserver{}
	b := New(Config{Worker: &mockWorker{}, WorkerID: "w", ManagerURL: srv.URL, Observer: obs})
	b.Start(context.Background())
	waitFor(t, "bridge stops on 404", func() bool { return !b.isRunning() })
	b.Stop()
	if got := obs.snapshot(); len(got) != 2 || got[0] != "attempt" || got[1] != "ended error" {
		t.Fatalf("calls = %v, want [attempt, ended error]", got)
	}
}

// A dial abandoned because the bridge is stopping is not a failure: the
// reference's runtime records no runtime_error when it is cancelled.
func TestObserverCancelledDialIsNotAnError(t *testing.T) {
	block := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { <-block }))
	defer srv.Close()
	defer close(block)
	obs := &recordingObserver{}
	b := New(Config{Worker: &mockWorker{}, WorkerID: "w", ManagerURL: srv.URL, Observer: obs})
	b.Start(context.Background())
	waitFor(t, "dial in flight", func() bool { return obs.has("attempt") })
	b.Stop()
	if got := obs.snapshot(); len(got) != 2 || got[1] != "ended" {
		t.Fatalf("calls = %v, want [attempt, ended]", got)
	}
}

// A frame the socket refused was never sent, so it is not reported.
func TestObserverNotToldOfAFailedWrite(t *testing.T) {
	client, _ := dialPair(t)
	_ = client.CloseNow()
	obs := &recordingObserver{}
	b := New(Config{Worker: &mockWorker{}, WorkerID: "w", ManagerURL: "http://x", Observer: obs})
	b.sendQ <- queuedFrame{control: map[string]any{"type": "ping"}}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { b.sendLoop(ctx, cancel, client); close(done) }()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("sendLoop did not return")
	}
	if got := obs.snapshot(); len(got) != 0 {
		t.Fatalf("a failed write was reported: %v", got)
	}
}

// Binary inbound messages reach the observer as the same latin-1 text the
// decoder is fed.
func TestObserverBinaryInboundIsReportedAsText(t *testing.T) {
	client, server := dialPair(t)
	obs := &recordingObserver{}
	b := New(Config{Worker: &mockWorker{session: &mockSession{}}, WorkerID: "w", ManagerURL: "http://x", Observer: obs})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go b.recvLoop(ctx, cancel, client)
	if err := server.Write(context.Background(), websocket.MessageBinary, []byte("k\xe9")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "binary reported", func() bool { return obs.has(`wire "ké"`) && obs.has(`input "ké"`) })
}

// A bridge configured without an observer gets one that ignores everything.
func TestDefaultObserverIgnoresEverything(t *testing.T) {
	b := New(Config{Worker: &mockWorker{}, WorkerID: "w", ManagerURL: "http://x"})
	o := b.observer
	o.AttemptStarted()
	errs := []error{
		o.Connected(),
		o.FrameSent("p", map[string]any{}),
		o.WireReceived("w"),
		o.ControlReceived(map[string]any{}),
		o.InputReceived("i"),
	}
	o.AttemptEnded(nil)
	for _, err := range errs {
		if err != nil {
			t.Fatalf("the default observer failed: %v", err)
		}
	}
	if _, ok := o.(nopObserver); !ok {
		t.Fatalf("default observer = %T", o)
	}
}

// endedWith is an observer that also keeps the error each attempt ended with.
type endedWith struct {
	recordingObserver
	errs []error
}

func (o *endedWith) AttemptEnded(err error) {
	o.mu.Lock()
	o.errs = append(o.errs, err)
	o.mu.Unlock()
	o.recordingObserver.AttemptEnded(err)
}

func (o *endedWith) firstErr() error {
	o.mu.Lock()
	defer o.mu.Unlock()
	if len(o.errs) == 0 {
		return nil
	}
	return o.errs[0]
}

// A served connection that the far end drops ends its attempt with the error
// that ended it, as the reference's run loop catches the socket's exception
// and records it as runtime_error.
func TestObserverSeesAConnectionEnd(t *testing.T) {
	hub := newFakeHub(t)
	obs := &endedWith{}
	b := New(Config{Worker: &mockWorker{session: &mockSession{}}, WorkerID: "w", ManagerURL: hub.baseURL(), Observer: obs})
	b.reconnectBackoff = []time.Duration{time.Hour}
	b.Start(context.Background())
	defer b.Stop()
	conn := hub.awaitConn(t)
	hub.awaitControl(t, "worker_hello")
	_ = conn.Close(websocket.StatusNormalClosure, "bye")
	waitFor(t, "the attempt to end", func() bool { return obs.has("ended error") })
	if err := obs.firstErr(); err == nil || !strings.Contains(err.Error(), "bye") {
		t.Fatalf("attempt ended with %v, want the close", err)
	}
}

// A stream the decoder rejects ends the attempt with the reference's
// "invalid control channel" error.
func TestObserverSeesAnInvalidControlChannel(t *testing.T) {
	hub := newFakeHub(t)
	obs := &endedWith{}
	b := New(Config{Worker: &mockWorker{session: &mockSession{}}, WorkerID: "w", ManagerURL: hub.baseURL(), Observer: obs})
	b.reconnectBackoff = []time.Duration{time.Hour}
	b.Start(context.Background())
	defer b.Stop()
	conn := hub.awaitConn(t)
	if err := conn.Write(context.Background(), websocket.MessageText, []byte{0x10, 0x03}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the attempt to end", func() bool { return obs.has("ended error") })
	if err := obs.firstErr(); err == nil || !strings.HasPrefix(err.Error(), "invalid control channel: ") {
		t.Fatalf("attempt ended with %v", err)
	}
}

// The first loop to fail names the end; the other's resulting cancellation
// does not overwrite it, and a new connection starts with a clean slate.
func TestConnectionEndKeepsTheFirstError(t *testing.T) {
	b := newBridge(&mockWorker{})
	b.resetConnErr()
	b.setConnErr(nil)
	b.setConnErr(errors.New("first"))
	b.setConnErr(errors.New("second"))
	if err := b.connErr(); err == nil || err.Error() != "first" {
		t.Fatalf("connErr = %v, want first", err)
	}
	b.resetConnErr()
	if err := b.connErr(); err != nil {
		t.Fatalf("connErr after reset = %v", err)
	}
}

// A write the socket refuses ends the connection with that error.
func TestSendLoopRecordsAWriteFailure(t *testing.T) {
	client, _ := dialPair(t)
	_ = client.CloseNow()
	b := newBridge(&mockWorker{})
	b.resetConnErr()
	b.sendQ <- queuedFrame{control: map[string]any{"type": "ping"}}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b.sendLoop(ctx, cancel, client)
	if b.connErr() == nil {
		t.Fatal("a failed write did not name the connection's end")
	}
}

// An observer that fails ends the connection, as a recording write that
// raises ends the reference's _bridge_session; what it failed on is the
// attempt's end. The reference logs before it acts, so input or a control
// message whose logging failed is not acted on.
func TestAFailingObserverEndsTheConnection(t *testing.T) {
	for _, tc := range []struct {
		method string
		drive  func(t *testing.T, hub *fakeHub, conn *websocket.Conn, session *mockSession)
		check  func(t *testing.T, worker *mockWorker, session *mockSession)
	}{
		{method: "Connected"},
		{method: "FrameSent"},
		{
			method: "WireReceived",
			drive:  func(t *testing.T, hub *fakeHub, c *websocket.Conn, _ *mockSession) { hub.writeData(t, c, "ls\r") },
			check: func(t *testing.T, _ *mockWorker, s *mockSession) {
				if len(s.sentKeys()) != 0 {
					t.Fatal("input was decoded and delivered after its logging failed")
				}
			},
		},
		{
			method: "InputReceived",
			drive:  func(t *testing.T, hub *fakeHub, c *websocket.Conn, _ *mockSession) { hub.writeData(t, c, "ls\r") },
			check: func(t *testing.T, _ *mockWorker, s *mockSession) {
				if len(s.sentKeys()) != 0 {
					t.Fatal("input was delivered after its logging failed")
				}
			},
		},
		{
			method: "ControlReceived",
			drive: func(t *testing.T, hub *fakeHub, c *websocket.Conn, _ *mockSession) {
				hub.writeControl(t, c, map[string]any{"type": "control", "action": "step"})
			},
			check: func(t *testing.T, w *mockWorker, _ *mockSession) {
				if w.steps() != 0 {
					t.Fatal("a control message was dispatched after its logging failed")
				}
			},
		},
	} {
		t.Run(tc.method, func(t *testing.T) {
			hub := newFakeHub(t)
			obs := &endedWith{}
			obs.failOn = tc.method
			session := &mockSession{}
			worker := &mockWorker{session: session}
			b := New(Config{Worker: worker, WorkerID: "w", ManagerURL: hub.baseURL(), Observer: obs})
			b.reconnectBackoff = []time.Duration{time.Hour}
			b.Start(context.Background())
			defer b.Stop()
			conn := hub.awaitConn(t)
			if tc.drive != nil {
				tc.drive(t, hub, conn, session)
			}
			waitFor(t, "the attempt to end", func() bool { return obs.has("ended error") })
			if err := obs.firstErr(); !errors.Is(err, errObserver) {
				t.Fatalf("attempt ended with %v, want the observer's error", err)
			}
			if tc.check != nil {
				tc.check(t, worker, session)
			}
		})
	}
}
