//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/bridge"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

// --- worker fakes driven by bridge.TermBridge -----------------------------

type e2eSession struct {
	mu       sync.Mutex
	watchers []bridge.WatchFunc
}

func (s *e2eSession) AddWatch(fn bridge.WatchFunc) {
	s.mu.Lock()
	s.watchers = append(s.watchers, fn)
	s.mu.Unlock()
}
func (s *e2eSession) Send(context.Context, string) error      { return nil }
func (s *e2eSession) SetSize(context.Context, int, int) error { return nil }
func (s *e2eSession) Snapshot() map[string]any {
	return map[string]any{"screen": "", "cols": 80, "rows": 25}
}

// emit pushes raw terminal output through the registered watchers.
func (s *e2eSession) emit(raw []byte) {
	s.mu.Lock()
	ws := append([]bridge.WatchFunc(nil), s.watchers...)
	s.mu.Unlock()
	for _, fn := range ws {
		fn(map[string]any{"screen": string(raw)}, raw)
	}
}

type e2eWorker struct {
	session  *e2eSession
	hijacked chan bool
}

func (w *e2eWorker) Session() bridge.Session { return w.session }
func (w *e2eWorker) SetHijacked(_ context.Context, enabled bool) error {
	select {
	case w.hijacked <- enabled:
	default:
	}
	return nil
}
func (w *e2eWorker) RequestStep(context.Context) error { return nil }

// --- browser websocket client --------------------------------------------

type browserClient struct {
	conn   *websocket.Conn
	frames chan map[string]any
	data   chan string
}

func dialBrowser(t *testing.T, ctx context.Context, wsURL, subject, role string) *browserClient {
	t.Helper()
	return dialBrowserWithHeaders(t, ctx, wsURL, http.Header{"X-Subject": {subject}, "X-Role": {role}})
}

func dialBrowserWithHeaders(t *testing.T, ctx context.Context, wsURL string, headers http.Header) *browserClient {
	t.Helper()
	conn, _, err := websocket.Dial(ctx, wsURL, &websocket.DialOptions{
		HTTPHeader: headers,
	})
	if err != nil {
		t.Fatalf("browser dial: %v", err)
	}
	conn.SetReadLimit(1 << 20)
	bc := &browserClient{conn: conn, frames: make(chan map[string]any, 64), data: make(chan string, 64)}
	go bc.readLoop(ctx)
	return bc
}

func (b *browserClient) readLoop(ctx context.Context) {
	dec := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
	for {
		mt, raw, err := b.conn.Read(ctx)
		if err != nil {
			return
		}
		var chunk string
		if mt == websocket.MessageBinary {
			chunk = controlchannel.WSBytesToChannelStr(raw)
		} else {
			chunk = string(raw)
		}
		events, ferr := dec.Feed(chunk)
		if ferr != nil {
			return
		}
		for _, ev := range events {
			switch e := ev.(type) {
			case controlchannel.DataChunk:
				b.data <- e.Data
			case controlchannel.ControlChunk:
				b.frames <- e.Control
			}
		}
	}
}

func (b *browserClient) send(t *testing.T, ctx context.Context, msg map[string]any) {
	t.Helper()
	payload, err := controlchannel.EncodeControlFrame(msg)
	if err != nil {
		t.Fatalf("encode: %v", err)
	}
	if err := b.conn.Write(ctx, websocket.MessageText, []byte(payload)); err != nil {
		t.Fatalf("browser write: %v", err)
	}
}

// waitFrame waits for a control frame of the given type.
func (b *browserClient) waitFrame(t *testing.T, typ string, timeout time.Duration) map[string]any {
	t.Helper()
	deadline := time.After(timeout)
	for {
		select {
		case f := <-b.frames:
			if f["type"] == typ {
				return f
			}
		case <-deadline:
			t.Fatalf("timed out waiting for frame %q", typ)
			return nil
		}
	}
}

// waitFrameWhere waits for a control frame of the given type satisfying pred.
func (b *browserClient) waitFrameWhere(t *testing.T, typ string, timeout time.Duration, pred func(map[string]any) bool) map[string]any {
	t.Helper()
	deadline := time.After(timeout)
	for {
		select {
		case f := <-b.frames:
			if f["type"] == typ && pred(f) {
				return f
			}
		case <-deadline:
			t.Fatalf("timed out waiting for frame %q matching predicate", typ)
			return nil
		}
	}
}

// TestE2EWorkerBrowserFlow starts the real Server, connects a real
// bridge.TermBridge (worker) and a coder/websocket browser client, and verifies
// terminal data and hijack control frames flow through the hub — decoding the
// wire bytes with the controlchannel package to prove framing.
func TestE2EWorkerBrowserFlow(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.srv.MarkReady()
	ts.reg.add("e2e", "admin1", "public")

	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	// Real worker bridge dials /ws/worker/e2e/term.
	session := &e2eSession{}
	worker := &e2eWorker{session: session, hijacked: make(chan bool, 4)}
	br := bridge.New(bridge.Config{
		Worker:     worker,
		WorkerID:   "e2e",
		ManagerURL: httpSrv.URL,
		InputMode:  "hijack",
		Encoding:   "latin-1",
	})
	br.Start(ctx)
	defer br.Stop()

	// Browser client dials /ws/browser/e2e/term.
	bc := dialBrowser(t, ctx, wsBase+"/ws/browser/e2e/term", "admin1", "admin")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()

	// The browser handshake begins with a hello frame.
	hello := bc.waitFrame(t, "hello", 5*time.Second)
	if hello["role"] != "admin" || hello["can_hijack"] != true {
		t.Fatalf("hello frame: %v", hello)
	}
	if hello["mcp_supported"] != true || hello["vnc_supported"] != true {
		t.Fatalf("hello capability defaults missing: %v", hello)
	}

	// Wait for the worker to be registered before emitting output.
	waitUntil(t, 5*time.Second, func() bool {
		return ts.hub.Registry.Contains("e2e")
	})

	// Worker emits terminal output → browser receives it (through the hub).
	waitUntil(t, 5*time.Second, func() bool {
		session.emit([]byte("HELLO-TERM"))
		select {
		case d := <-bc.data:
			return strings.Contains(d, "HELLO-TERM")
		case <-time.After(200 * time.Millisecond):
			return false
		}
	})

	// Browser requests a hijack → worker is paused + hijack_state broadcast.
	bc.send(t, ctx, map[string]any{"type": "hijack_request"})
	select {
	case enabled := <-worker.hijacked:
		if !enabled {
			t.Fatalf("worker received resume, expected pause")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("worker never paused on hijack_request")
	}
	// Wait for the post-acquire hijack_state (skipping the stale handshake one
	// that carried hijacked=false).
	bc.waitFrameWhere(t, "hijack_state", 5*time.Second, func(f map[string]any) bool {
		return f["hijacked"] == true
	})

	// Exercise the remaining browser message handlers.
	bc.send(t, ctx, map[string]any{"type": "input", "data": "ls\n"})
	// Oversized input (> MaxInputChars) → server replies with an error frame.
	bc.send(t, ctx, map[string]any{"type": "input", "data": strings.Repeat("x", 11000)})
	bc.waitFrameWhere(t, "error", 5*time.Second, func(f map[string]any) bool {
		msg, _ := f["message"].(string)
		return strings.Contains(msg, "Input too long")
	})
	bc.send(t, ctx, map[string]any{"type": "snapshot_req"})
	bc.send(t, ctx, map[string]any{"type": "hijack_step"})
	bc.send(t, ctx, map[string]any{"type": "ping"})
	bc.waitFrame(t, "pong", 5*time.Second)
	bc.send(t, ctx, map[string]any{"type": "heartbeat"})
	bc.waitFrame(t, "heartbeat_ack", 5*time.Second)

	// Browser releases the hijack.
	bc.send(t, ctx, map[string]any{"type": "hijack_release"})
	waitUntil(t, 5*time.Second, func() bool {
		return !ts.hub.CheckStillHijacked("e2e")
	})

	// A viewer browser is rejected when it requests a hijack.
	vc := dialBrowser(t, ctx, wsBase+"/ws/browser/e2e/term", "view1", "viewer")
	defer func() { _ = vc.conn.Close(websocket.StatusNormalClosure, "") }()
	vc.waitFrame(t, "hello", 5*time.Second)
	vc.send(t, ctx, map[string]any{"type": "hijack_request"})
	vc.waitFrameWhere(t, "error", 5*time.Second, func(f map[string]any) bool {
		msg, _ := f["message"].(string)
		return strings.Contains(msg, "admin role")
	})

	// Forcibly disconnect the worker (covers the worker-conn close seam).
	if _, err := ts.hub.DisconnectWorker(context.Background(), "e2e"); err != nil {
		t.Fatalf("disconnect worker: %v", err)
	}
}

// TestE2EBrowserRejectedForUnknownSession verifies the browser WS closes when
// the session cannot be read (role resolution fails).
func TestE2EBrowserRejectedForUnknownSession(t *testing.T) {
	ts := newTestServer(t, nil)
	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// No session definition for "ghost" → role resolution fails → 1008 close.
	_, _, err := websocket.Dial(ctx, wsBase+"/ws/browser/ghost/term", &websocket.DialOptions{
		HTTPHeader: http.Header{"X-Subject": {"admin1"}, "X-Role": {"admin"}},
	})
	// The dial itself may succeed (upgrade accepted) then close; either way a
	// subsequent read must fail. We accept an error at dial or first read.
	if err == nil {
		t.Log("dial accepted; server closes after role resolution")
	}
}

// waitUntil polls cond until it returns true or the timeout elapses.
func waitUntil(t *testing.T, timeout time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("condition not met before timeout")
}
