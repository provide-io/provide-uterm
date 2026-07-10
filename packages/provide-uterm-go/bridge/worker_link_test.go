//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package bridge

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

// newBridge builds a TermBridge with a discard logger for a mock worker.
func newBridge(w Worker) *TermBridge {
	return New(Config{Worker: w, WorkerID: "bot1", ManagerURL: "http://localhost:8000"})
}

// drainControl reads the next queued frame, asserting it is a control frame,
// and returns its payload.
func drainControl(t *testing.T, b *TermBridge) map[string]any {
	t.Helper()
	select {
	case f := <-b.sendQ:
		if f.isTerm {
			t.Fatalf("expected control frame, got term %q", f.data)
		}
		return f.control
	case <-time.After(time.Second):
		t.Fatal("no frame queued")
		return nil
	}
}

// wsScheme rewrites an httptest http:// URL to ws://.
func wsScheme(httpURL string) string { return "ws" + strings.TrimPrefix(httpURL, "http") }

// dialPair returns a connected client/server conn pair kept alive until cleanup.
func dialPair(t *testing.T) (client, server *websocket.Conn) {
	t.Helper()
	srvCh := make(chan *websocket.Conn, 1)
	block := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		srvCh <- c
		<-block
	}))
	client, _, err := websocket.Dial(context.Background(), wsScheme(srv.URL), nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	server = <-srvCh
	t.Cleanup(func() {
		_ = client.CloseNow()
		_ = server.CloseNow()
		close(block)
		srv.Close()
	})
	return client, server
}

// ---------------------------------------------------------------------------
// AttachSession
// ---------------------------------------------------------------------------

func TestAttachSession(t *testing.T) {
	session := &mockSession{}
	b := newBridge(&mockWorker{session: session})
	b.AttachSession()
	b.AttachSession() // idempotent
	if session.watchCount() != 1 {
		t.Fatalf("expected 1 watch, got %d", session.watchCount())
	}

	// The watch queues term data and updates the latest snapshot.
	session.fire(map[string]any{"screen": "s"}, []byte("Hello"))
	f := <-b.sendQ
	if !f.isTerm || f.data != "Hello" {
		t.Fatalf("expected term frame Hello, got %+v", f)
	}
	b.mu.Lock()
	latest := b.latestSnapshot
	b.mu.Unlock()
	if latest["screen"] != "s" {
		t.Fatalf("latest snapshot not updated: %v", latest)
	}

	// Empty raw does not queue anything.
	session.fire(map[string]any{"screen": "s2"}, nil)
	select {
	case f := <-b.sendQ:
		t.Fatalf("empty raw should not queue, got %+v", f)
	default:
	}
}

func TestAttachSessionNoSession(t *testing.T) {
	b := newBridge(&mockWorker{session: nil})
	b.AttachSession() // no-op, must not panic
}

func TestAttachSessionWatchDropsWhenFull(t *testing.T) {
	session := &mockSession{}
	b := newBridge(&mockWorker{session: session})
	b.AttachSession()
	// Fill the queue.
	for len(b.sendQ) < cap(b.sendQ) {
		b.sendQ <- queuedFrame{isTerm: true, data: "x"}
	}
	// This fire is dropped (queue full) — must not block or panic.
	session.fire(map[string]any{"screen": "s"}, []byte("dropped"))
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

func TestStartIdempotentAndStop(t *testing.T) {
	// Point at a dead address so the run loop just backs off; we only assert
	// Start/Stop bookkeeping.
	b := New(Config{Worker: &mockWorker{}, WorkerID: "bot1", ManagerURL: "http://127.0.0.1:1"})
	b.reconnectBackoff = []time.Duration{10 * time.Millisecond}
	ctx := context.Background()
	b.Start(ctx)
	if !b.isRunning() {
		t.Fatal("should be running after Start")
	}
	b.Start(ctx) // idempotent — no second goroutine
	b.Stop()
	if b.isRunning() {
		t.Fatal("should not be running after Stop")
	}
}

// ---------------------------------------------------------------------------
// encodeQueuedFrame
// ---------------------------------------------------------------------------

func TestEncodeQueuedFrame(t *testing.T) {
	term, err := encodeQueuedFrame(queuedFrame{isTerm: true, data: "abc"})
	if err != nil || term != controlchannel.EncodeTerminalData("abc") {
		t.Fatalf("term encode: %q err=%v", term, err)
	}
	ctrl, err := encodeQueuedFrame(queuedFrame{control: map[string]any{"type": "ping"}})
	if err != nil || !controlchannel.IsControlFrame(ctrl) {
		t.Fatalf("control encode: %q err=%v", ctrl, err)
	}
	// A payload with an unmarshalable value surfaces an error.
	if _, err := encodeQueuedFrame(queuedFrame{control: map[string]any{"bad": make(chan int)}}); err == nil {
		t.Fatal("expected encode error for unmarshalable payload")
	}
}

// ---------------------------------------------------------------------------
// dispatchControl (connection-free branches)
// ---------------------------------------------------------------------------

func TestDispatchControl(t *testing.T) {
	ctx := context.Background()
	session := &mockSession{snapshot: map[string]any{"screen": "scr"}}
	worker := &mockWorker{session: session}
	b := newBridge(worker)

	// control pause / resume / step
	b.dispatchControl(ctx, map[string]any{"type": "control", "action": "pause"})
	b.dispatchControl(ctx, map[string]any{"type": "control", "action": "resume"})
	b.dispatchControl(ctx, map[string]any{"type": "control", "action": "step"})
	if got := worker.calls(); len(got) != 2 || got[0] != true || got[1] != false {
		t.Fatalf("hijack calls = %v, want [true false]", got)
	}
	if worker.steps() != 1 {
		t.Fatalf("step calls = %d, want 1", worker.steps())
	}
	// An unknown control action is ignored.
	b.dispatchControl(ctx, map[string]any{"type": "control", "action": "wat"})

	// resize
	b.dispatchControl(ctx, map[string]any{"type": "resize", "cols": float64(132), "rows": float64(50)})
	if sizes := session.allSizes(); len(sizes) != 1 || sizes[0] != [2]int{132, 50} {
		t.Fatalf("sizes = %v", session.allSizes())
	}

	// session_token captures the resume token.
	b.dispatchControl(ctx, map[string]any{"type": "session_token", "token": "tok1"})
	if b.ResumeToken() != "tok1" {
		t.Fatalf("resume token = %q, want tok1", b.ResumeToken())
	}
	// A blank session_token is ignored.
	b.dispatchControl(ctx, map[string]any{"type": "session_token", "token": ""})
	if b.ResumeToken() != "tok1" {
		t.Fatal("blank token should not overwrite")
	}

	// resume_ok / resume_failed are logged only.
	b.dispatchControl(ctx, map[string]any{"type": "resume_ok"})
	b.dispatchControl(ctx, map[string]any{"type": "resume_failed", "reason": "nope"})

	// snapshot_req enqueues a snapshot.
	drainAll(b)
	b.dispatchControl(ctx, map[string]any{"type": "snapshot_req"})
	snap := drainControl(t, b)
	if snap["type"] != "snapshot" || snap["screen"] != "scr" {
		t.Fatalf("snapshot frame = %v", snap)
	}
}

func TestDispatchControlCustomHandler(t *testing.T) {
	ctx := context.Background()
	b := newBridge(&mockWorker{})
	got := make(chan map[string]any, 1)
	b.RegisterMessageHandler("custom_thing", func(_ context.Context, msg map[string]any) error {
		got <- msg
		return nil
	})
	b.dispatchControl(ctx, map[string]any{"type": "custom_thing", "x": float64(1)})
	select {
	case msg := <-got:
		if msg["x"] != float64(1) {
			t.Fatalf("handler got %v", msg)
		}
	case <-time.After(time.Second):
		t.Fatal("custom handler not invoked")
	}

	// A handler returning an error is logged, not propagated.
	b.RegisterMessageHandler("boom", func(_ context.Context, _ map[string]any) error {
		return errors.New("kaboom")
	})
	b.dispatchControl(ctx, map[string]any{"type": "boom"})

	// An unknown type with no handler is silently ignored.
	b.dispatchControl(ctx, map[string]any{"type": "no_handler"})
	// A message with a non-string type is ignored.
	b.dispatchControl(ctx, map[string]any{"type": 5})
}

// ---------------------------------------------------------------------------
// sendSnapshot
// ---------------------------------------------------------------------------

func TestSendSnapshotNoSession(t *testing.T) {
	b := newBridge(&mockWorker{session: nil})
	b.sendSnapshot()
	if len(b.sendQ) != 0 {
		t.Fatal("no session → no snapshot queued")
	}
}

func TestSendSnapshotFromEmulator(t *testing.T) {
	session := &mockSession{snapshot: map[string]any{
		"screen": "live", "cols": float64(100), "rows": float64(40),
		"screen_hash": "h", "cursor_at_end": true, "has_trailing_space": true,
		"prompt_detected": map[string]any{"id": "p"},
	}}
	b := newBridge(&mockWorker{session: session})
	b.sendSnapshot()
	snap := drainControl(t, b)
	if snap["screen"] != "live" || snap["cols"] != 100 || snap["rows"] != 40 {
		t.Fatalf("snapshot = %v", snap)
	}
	if snap["cursor_at_end"] != true || snap["has_trailing_space"] != true {
		t.Fatalf("snapshot bools = %v", snap)
	}
}

func TestSendSnapshotFallsBackToLatest(t *testing.T) {
	session := &mockSession{snapshot: nil}
	b := newBridge(&mockWorker{session: session})
	b.AttachSession()
	session.fire(map[string]any{"screen": "cached", "cols": float64(80), "rows": float64(25)}, []byte("x"))
	drainAll(b) // discard the term frame from the fire
	b.sendSnapshot()
	snap := drainControl(t, b)
	if snap["screen"] != "cached" {
		t.Fatalf("expected cached screen, got %v", snap["screen"])
	}
}

func TestSendSnapshotEmpty(t *testing.T) {
	session := &mockSession{snapshot: nil}
	b := newBridge(&mockWorker{session: session})
	b.sendSnapshot()
	snap := drainControl(t, b)
	if snap["screen"] != "" || snap["cols"] != 80 || snap["rows"] != 25 {
		t.Fatalf("empty snapshot defaults wrong: %v", snap)
	}
}

// ---------------------------------------------------------------------------
// worker-facing helpers
// ---------------------------------------------------------------------------

func TestSendKeys(t *testing.T) {
	ctx := context.Background()
	// Verbatim forwarding — no CR conversion.
	session := &mockSession{}
	b := newBridge(&mockWorker{session: session})
	b.sendKeys(ctx, "hello\\r")
	b.sendKeys(ctx, "hello\r")
	if keys := session.sentKeys(); len(keys) != 2 || keys[0] != "hello\\r" || keys[1] != "hello\r" {
		t.Fatalf("sent = %q", session.sentKeys())
	}
	// No session → no-op.
	newBridge(&mockWorker{session: nil}).sendKeys(ctx, "x")
	// Session send error is swallowed.
	errSession := &mockSession{sendErr: errors.New("down")}
	newBridge(&mockWorker{session: errSession}).sendKeys(ctx, "x")
}

func TestSetSize(t *testing.T) {
	ctx := context.Background()
	session := &mockSession{}
	b := newBridge(&mockWorker{session: session})
	b.setSize(ctx, 80, 25)
	if sizes := session.allSizes(); len(sizes) != 1 || sizes[0] != [2]int{80, 25} {
		t.Fatalf("sizes = %v", session.allSizes())
	}
	newBridge(&mockWorker{session: nil}).setSize(ctx, 1, 1) // no-op
	errSession := &mockSession{sizeErr: errors.New("down")}
	newBridge(&mockWorker{session: errSession}).setSize(ctx, 1, 1) // swallowed
}

func TestRequestStep(t *testing.T) {
	ctx := context.Background()
	worker := &mockWorker{}
	newBridge(worker).requestStep(ctx)
	if worker.steps() != 1 {
		t.Fatal("request_step should call worker")
	}
	// Error is swallowed.
	newBridge(&mockWorker{stepErr: errors.New("x")}).requestStep(ctx)
}

func TestSetHijacked(t *testing.T) {
	ctx := context.Background()
	worker := &mockWorker{}
	b := newBridge(worker)
	b.setHijacked(ctx, true)
	if got := worker.calls(); len(got) != 1 || got[0] != true {
		t.Fatalf("hijack calls = %v", got)
	}
	status := drainControl(t, b)
	if status["type"] != "status" || status["hijacked"] != true {
		t.Fatalf("status frame = %v", status)
	}
	// A worker that errors still enqueues the status frame.
	errWorker := &mockWorker{hijackErr: errors.New("bot exploded")}
	b2 := newBridge(errWorker)
	b2.setHijacked(ctx, false)
	status2 := drainControl(t, b2)
	if status2["hijacked"] != false {
		t.Fatalf("status frame = %v", status2)
	}
}

func TestEnqueueHelloWithCapabilities(t *testing.T) {
	b := New(Config{
		Worker: &mockWorker{}, WorkerID: "w", ManagerURL: "http://x",
		InputMode: "hijack", Capabilities: map[string]any{"feat": true},
	})
	b.enqueueHello()
	hello := drainControl(t, b)
	if hello["type"] != "worker_hello" || hello["input_mode"] != "hijack" {
		t.Fatalf("hello = %v", hello)
	}
	proto, _ := hello["protocol"].(map[string]any)
	if proto["min"] != MinProtocolVersion || proto["max"] != MaxProtocolVersion || proto["preferred"] != PreferredProtocolVersion {
		t.Fatalf("hello protocol = %v", proto)
	}
	if _, ok := hello["capabilities"]; !ok {
		t.Fatal("capabilities should be advertised when set")
	}
}

// drainAll empties the send queue.
func drainAll(b *TermBridge) {
	for {
		select {
		case <-b.sendQ:
		default:
			return
		}
	}
}

func TestEncodeTermBytesEncodings(t *testing.T) {
	raw := []byte{0xC9, 0xCD, 0xBB}
	cp := New(Config{Worker: &mockWorker{}, WorkerID: "w", ManagerURL: "http://x"})
	if got := cp.encodeTermBytes(raw); got != "╔═╗" {
		t.Fatalf("cp437 got %q", got)
	}
	lat := New(Config{Worker: &mockWorker{}, WorkerID: "w", ManagerURL: "http://x", Encoding: "latin-1"})
	if got := lat.encodeTermBytes(raw); got != "ÉÍ»" {
		t.Fatalf("latin-1 got %q", got)
	}
}
