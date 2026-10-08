//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package bridge

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
)

// recordingObserver logs every Observer call as one line, in order.
type recordingObserver struct {
	mu    sync.Mutex
	calls []string
}

func (o *recordingObserver) add(format string, args ...any) {
	o.mu.Lock()
	o.calls = append(o.calls, fmt.Sprintf(format, args...))
	o.mu.Unlock()
}

func (o *recordingObserver) AttemptStarted() { o.add("attempt") }
func (o *recordingObserver) Connected()      { o.add("connected") }
func (o *recordingObserver) FrameSent(payload string, frame map[string]any) {
	if frame["type"] == "term" {
		o.add("sent term %q payload=%t", frame["data"], strings.Contains(payload, frame["data"].(string)))
		return
	}
	o.add("sent %v", frame["type"])
}
func (o *recordingObserver) WireReceived(text string)           { o.add("wire %q", text) }
func (o *recordingObserver) ControlReceived(msg map[string]any) { o.add("control %v", msg["type"]) }
func (o *recordingObserver) InputReceived(data string)          { o.add("input %q", data) }
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
	o.Connected()
	o.FrameSent("p", map[string]any{})
	o.WireReceived("w")
	o.ControlReceived(map[string]any{})
	o.InputReceived("i")
	o.AttemptEnded(nil)
	if _, ok := o.(nopObserver); !ok {
		t.Fatalf("default observer = %T", o)
	}
}
