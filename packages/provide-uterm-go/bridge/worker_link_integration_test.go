//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package bridge

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

// ---------------------------------------------------------------------------
// Focused send/recv loop tests over a real connected conn pair
// ---------------------------------------------------------------------------

func TestSendLoopWritesAndSkipsBadFrames(t *testing.T) {
	client, server := dialPair(t)
	b := newBridge(&mockWorker{})
	// A bad frame (unmarshalable) is skipped; the good frame is written.
	b.sendQ <- queuedFrame{control: map[string]any{"bad": make(chan int)}}
	b.sendQ <- queuedFrame{control: map[string]any{"type": "ping"}}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go b.sendLoop(ctx, cancel, client)

	_, data, err := server.Read(context.Background())
	if err != nil {
		t.Fatalf("server read: %v", err)
	}
	frame, err := decodeSingleControl(string(data))
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if frame["type"] != "ping" {
		t.Fatalf("expected the good ping frame, got %v", frame)
	}
}

func TestSendLoopNetworkError(t *testing.T) {
	client, _ := dialPair(t)
	_ = client.CloseNow() // any write now fails
	b := newBridge(&mockWorker{})
	b.sendQ <- queuedFrame{control: map[string]any{"type": "ping"}}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { b.sendLoop(ctx, cancel, client); close(done) }()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("sendLoop did not return on a write error")
	}
}

func TestRecvLoopReadErrorClearsHijack(t *testing.T) {
	client, server := dialPair(t)
	worker := &mockWorker{}
	b := newBridge(worker)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { b.recvLoop(ctx, cancel, client); close(done) }()
	_ = server.CloseNow() // client Read errors
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("recvLoop did not return on read error")
	}
	// The finally self-clears the hijack.
	if calls := worker.calls(); len(calls) != 1 || calls[0] != false {
		t.Fatalf("expected a single SetHijacked(false), got %v", calls)
	}
}

func TestRecvLoopBadStreamReturns(t *testing.T) {
	client, server := dialPair(t)
	b := newBridge(&mockWorker{})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { b.recvLoop(ctx, cancel, client); close(done) }()
	// DLE followed by a non-STX, non-DLE byte → invalid control prefix.
	if err := server.Write(context.Background(), websocket.MessageText, []byte{0x10, 0x03}); err != nil {
		t.Fatalf("server write: %v", err)
	}
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("recvLoop did not return on a malformed stream")
	}
}

func TestRecvLoopBinaryDataForwardsKeys(t *testing.T) {
	client, server := dialPair(t)
	session := &mockSession{}
	b := newBridge(&mockWorker{session: session})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go b.recvLoop(ctx, cancel, client)
	if err := server.Write(context.Background(), websocket.MessageBinary, []byte("keys")); err != nil {
		t.Fatalf("server write: %v", err)
	}
	waitFor(t, "keys forwarded", func() bool {
		keys := session.sentKeys()
		return len(keys) == 1 && keys[0] == "keys"
	})
}

// ---------------------------------------------------------------------------
// Fake hub + end-to-end drive
// ---------------------------------------------------------------------------

// fakeHub is an httptest WebSocket server that speaks the control channel and
// records everything a worker sends it.
type fakeHub struct {
	srv    *httptest.Server
	mu     sync.Mutex
	ctrl   []map[string]any
	data   []string
	connCh chan *websocket.Conn
}

func newFakeHub(t *testing.T) *fakeHub {
	t.Helper()
	h := &fakeHub{connCh: make(chan *websocket.Conn, 4)}
	h.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		h.connCh <- c
		h.readLoop(c)
	}))
	t.Cleanup(h.srv.Close)
	return h
}

func (h *fakeHub) readLoop(c *websocket.Conn) {
	decoder := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
	ctx := context.Background()
	for {
		typ, raw, err := c.Read(ctx)
		if err != nil {
			return
		}
		var chunk string
		if typ == websocket.MessageBinary {
			chunk = controlchannel.WSBytesToChannelStr(raw)
		} else {
			chunk = string(raw)
		}
		events, err := decoder.Feed(chunk)
		if err != nil {
			return
		}
		h.mu.Lock()
		for _, ev := range events {
			switch e := ev.(type) {
			case controlchannel.ControlChunk:
				h.ctrl = append(h.ctrl, e.Control)
			case controlchannel.DataChunk:
				h.data = append(h.data, e.Data)
			}
		}
		h.mu.Unlock()
	}
}

func (h *fakeHub) baseURL() string {
	// httptest serves http://; the bridge converts it to ws://.
	return h.srv.URL
}

func (h *fakeHub) awaitConn(t *testing.T) *websocket.Conn {
	t.Helper()
	select {
	case c := <-h.connCh:
		return c
	case <-time.After(2 * time.Second):
		t.Fatal("worker never connected")
		return nil
	}
}

func (h *fakeHub) writeControl(t *testing.T, c *websocket.Conn, payload map[string]any) {
	t.Helper()
	frame, err := controlchannel.EncodeControlFrame(payload)
	if err != nil {
		t.Fatalf("encode control: %v", err)
	}
	if err := c.Write(context.Background(), websocket.MessageText, []byte(frame)); err != nil {
		t.Fatalf("hub write: %v", err)
	}
}

func (h *fakeHub) writeData(t *testing.T, c *websocket.Conn, data string) {
	t.Helper()
	frame := controlchannel.EncodeTerminalData(data)
	if err := c.Write(context.Background(), websocket.MessageText, []byte(frame)); err != nil {
		t.Fatalf("hub write data: %v", err)
	}
}

// awaitControl polls for the first recorded control frame of the given type.
func (h *fakeHub) awaitControl(t *testing.T, frameType string) map[string]any {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		h.mu.Lock()
		for _, f := range h.ctrl {
			if f["type"] == frameType {
				h.mu.Unlock()
				return f
			}
		}
		h.mu.Unlock()
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("no %q control frame arrived", frameType)
	return nil
}

func (h *fakeHub) hasData(want string) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, d := range h.data {
		if strings.Contains(d, want) {
			return true
		}
	}
	return false
}

// waitFor polls cond until true or a 2s timeout.
func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func TestTermBridgeEndToEnd(t *testing.T) {
	hub := newFakeHub(t)
	session := &mockSession{snapshot: map[string]any{"screen": "HELLO", "cols": float64(80), "rows": float64(25)}}
	worker := &mockWorker{session: session}
	b := New(Config{
		Worker:            worker,
		WorkerID:          "bot1",
		ManagerURL:        hub.baseURL(),
		InputMode:         "open",
		ResumeToken:       "seed-token",
		Capabilities:      map[string]any{"cap": true},
		HeartbeatInterval: 15 * time.Millisecond,
	})
	greeted := make(chan struct{}, 1)
	b.RegisterMessageHandler("greet", func(_ context.Context, _ map[string]any) error {
		greeted <- struct{}{}
		return nil
	})

	b.Start(context.Background())
	defer b.Stop()

	conn := hub.awaitConn(t)

	// worker_hello carries the protocol range, input_mode and capabilities.
	hello := hub.awaitControl(t, "worker_hello")
	if hello["input_mode"] != "open" {
		t.Fatalf("hello input_mode = %v", hello["input_mode"])
	}
	proto, _ := hello["protocol"].(map[string]any)
	if proto["min"] != float64(MinProtocolVersion) || proto["max"] != float64(MaxProtocolVersion) {
		t.Fatalf("hello protocol = %v", proto)
	}
	if _, ok := hello["capabilities"]; !ok {
		t.Fatal("hello should carry capabilities")
	}

	// The seeded resume token is sent on connect.
	if resume := hub.awaitControl(t, "resume"); resume["token"] != "seed-token" {
		t.Fatalf("resume token = %v", resume["token"])
	}

	// snapshot_req → snapshot.
	hub.writeControl(t, conn, map[string]any{"type": "snapshot_req"})
	if snap := hub.awaitControl(t, "snapshot"); snap["screen"] != "HELLO" {
		t.Fatalf("snapshot screen = %v", snap["screen"])
	}

	// Input terminal data → forwarded to the session.
	hub.writeData(t, conn, "ls\r")
	waitFor(t, "input forwarded", func() bool {
		keys := session.sentKeys()
		return len(keys) == 1 && keys[0] == "ls\r"
	})

	// pause / resume / step.
	hub.writeControl(t, conn, map[string]any{"type": "control", "action": "pause"})
	hub.writeControl(t, conn, map[string]any{"type": "control", "action": "resume"})
	hub.writeControl(t, conn, map[string]any{"type": "control", "action": "step"})
	waitFor(t, "hijack control applied", func() bool {
		calls := worker.calls()
		return len(calls) >= 2 && calls[0] == true && calls[1] == false && worker.steps() >= 1
	})
	// A status frame is emitted for the pause.
	hub.awaitControl(t, "status")

	// resize.
	hub.writeControl(t, conn, map[string]any{"type": "resize", "cols": float64(132), "rows": float64(50)})
	waitFor(t, "resize applied", func() bool {
		sizes := session.allSizes()
		return len(sizes) >= 1 && sizes[len(sizes)-1] == [2]int{132, 50}
	})

	// session_token updates the resume token.
	hub.writeControl(t, conn, map[string]any{"type": "session_token", "token": "tok-new"})
	waitFor(t, "resume token updated", func() bool { return b.ResumeToken() == "tok-new" })

	// custom handler.
	hub.writeControl(t, conn, map[string]any{"type": "greet"})
	select {
	case <-greeted:
	case <-time.After(2 * time.Second):
		t.Fatal("custom handler not invoked")
	}

	// Terminal output from the session is forwarded to the hub.
	session.fire(map[string]any{"screen": "x"}, []byte("screen-bytes"))
	waitFor(t, "term forwarded", func() bool { return hub.hasData("screen-bytes") })

	// The inline heartbeat is delivered.
	hub.awaitControl(t, "heartbeat")
}

// ---------------------------------------------------------------------------
// Reconnect loop / permanent errors
// ---------------------------------------------------------------------------

func TestRunStopsOnPermanentError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()
	b := New(Config{Worker: &mockWorker{}, WorkerID: "w", ManagerURL: srv.URL})
	b.reconnectBackoff = []time.Duration{time.Millisecond}
	b.Start(context.Background())
	waitFor(t, "bridge stops on 404", func() bool { return !b.isRunning() })
	b.Stop()
}

func TestRunStopsOnMalformedURL(t *testing.T) {
	b := New(Config{Worker: &mockWorker{}, WorkerID: "w", ManagerURL: "ftp://nope"})
	b.Start(context.Background())
	waitFor(t, "bridge stops on malformed URL", func() bool { return !b.isRunning() })
	b.Stop()
}

func TestRunReconnects(t *testing.T) {
	var conns int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		atomic.AddInt32(&conns, 1)
		_ = c.CloseNow() // drop immediately → worker reconnects
	}))
	defer srv.Close()
	b := New(Config{Worker: &mockWorker{}, WorkerID: "w", ManagerURL: srv.URL})
	b.reconnectBackoff = []time.Duration{time.Millisecond}
	b.Start(context.Background())
	waitFor(t, "worker reconnects", func() bool { return atomic.LoadInt32(&conns) >= 2 })
	b.Stop()
}

func TestRunCancelDuringBackoff(t *testing.T) {
	var conns int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		atomic.AddInt32(&conns, 1)
		_ = c.CloseNow()
	}))
	defer srv.Close()
	b := New(Config{Worker: &mockWorker{}, WorkerID: "w", ManagerURL: srv.URL})
	// A long backoff parks the run loop in the timer select after the first
	// disconnect; Stop then cancels the context and exercises the ctx.Done exit.
	b.reconnectBackoff = []time.Duration{10 * time.Second}
	b.Start(context.Background())
	waitFor(t, "one connection made", func() bool { return atomic.LoadInt32(&conns) >= 1 })
	time.Sleep(50 * time.Millisecond) // let the loop settle into the backoff timer
	b.Stop()
}

// decodeSingleControl decodes one control frame string into its payload.
func decodeSingleControl(s string) (map[string]any, error) {
	decoder := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
	events, err := decoder.Feed(s)
	if err != nil {
		return nil, err
	}
	for _, ev := range events {
		if c, ok := ev.(controlchannel.ControlChunk); ok {
			return c.Control, nil
		}
	}
	return nil, errNoControlFrame
}

var errNoControlFrame = &decodeError{"no control frame decoded"}

type decodeError struct{ msg string }

func (e *decodeError) Error() string { return e.msg }
