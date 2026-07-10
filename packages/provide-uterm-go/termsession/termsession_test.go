//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package termsession

import (
	"context"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/session"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/transports"
)

// fakeTransport is a scriptable ConnectionTransport: Receive pops queued
// chunks, then times out (empty) until closed.
type fakeTransport struct {
	mu        sync.Mutex
	chunks    [][]byte
	sent      [][]byte
	recvErr   error
	sendErr   error
	connected bool
}

func (f *fakeTransport) Connect(context.Context, string, int, transports.ConnectOptions) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.connected = true
	return nil
}

func (f *fakeTransport) Disconnect(context.Context) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.connected = false
	return nil
}

func (f *fakeTransport) Send(_ context.Context, data []byte) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.sent = append(f.sent, append([]byte(nil), data...))
	return f.sendErr
}

func (f *fakeTransport) Receive(_ context.Context, _ int, timeout time.Duration) ([]byte, error) {
	f.mu.Lock()
	if f.recvErr != nil {
		err := f.recvErr
		f.mu.Unlock()
		return nil, err
	}
	if len(f.chunks) > 0 {
		chunk := f.chunks[0]
		f.chunks = f.chunks[1:]
		f.mu.Unlock()
		return chunk, nil
	}
	f.mu.Unlock()
	time.Sleep(min(timeout, time.Millisecond))
	return nil, nil
}

func (f *fakeTransport) IsConnected() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.connected
}

func (f *fakeTransport) queue(chunks ...[]byte) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.chunks = append(f.chunks, chunks...)
}

func newFakeSession(t *testing.T, ft *fakeTransport, opts Options) *TransportSession {
	t.Helper()
	s := New(ft, func(ctx context.Context) error {
		return ft.Connect(ctx, "", 0, transports.ConnectOptions{})
	}, opts)
	if err := s.Connect(context.Background()); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Close(context.Background()) })
	return s
}

func waitForScreen(t *testing.T, s *TransportSession, substr string) session.Snapshot {
	t.Helper()
	// 30s (not 10s): under heavy CPU contention (e.g. a nested-virtualization
	// container running the whole-module -race suite at once), the real PTY
	// negotiate-then-echo round trip in TestTelnetSessionLoopback can
	// genuinely take longer than 10s to complete — a generous safety net,
	// not protocol timing.
	deadline := time.Now().Add(30 * time.Second)
	for {
		snap := s.Snapshot()
		if strings.Contains(snap.Screen, substr) {
			return snap
		}
		if time.Now().After(deadline) {
			t.Fatalf("screen never contained %q: %q", substr, snap.Screen)
		}
		_, _ = s.WaitForUpdate(context.Background(), 50*time.Millisecond)
	}
}

func TestReaderFeedsEmulatorAndSignalsUpdates(t *testing.T) {
	ft := &fakeTransport{}
	s := newFakeSession(t, ft, Options{Cols: 20, Rows: 4})
	seq0 := s.ScreenChangeSeq()
	ft.queue([]byte("hello prompt> "))
	snap := waitForScreen(t, s, "hello prompt>")
	if snap.Cols != 20 || snap.Rows != 4 {
		t.Fatalf("snap = %+v", snap)
	}
	if s.ScreenChangeSeq() <= seq0 {
		t.Fatal("change seq did not advance")
	}
	if !s.IsConnected() {
		t.Fatal("not connected")
	}
	if !strings.Contains(s.ANSIScreen(), "hello prompt>") {
		t.Fatal("ANSI screen missing content")
	}
}

func TestWatchersSeeRawBytesBeforeEmulator(t *testing.T) {
	ft := &fakeTransport{}
	s := newFakeSession(t, ft, Options{})
	var mu sync.Mutex
	var got []byte
	s.AddWatch(func(state map[string]any, raw []byte) {
		mu.Lock()
		defer mu.Unlock()
		if len(state) != 0 {
			t.Error("state must be empty")
		}
		got = append(got, raw...)
	})
	// A panicking watcher must not kill the reader.
	s.AddWatch(func(map[string]any, []byte) { panic("boom") })
	raw := []byte("\x1b[31mred\x1b[0m")
	ft.queue(raw)
	waitForScreen(t, s, "red")
	mu.Lock()
	defer mu.Unlock()
	if string(got) != string(raw) {
		t.Fatalf("watcher got %q want %q", got, raw)
	}
}

func TestSendEncodings(t *testing.T) {
	ft := &fakeTransport{}
	s := newFakeSession(t, ft, Options{SendEncoding: EncodingCP437})
	if err := s.Send(context.Background(), "╔→"); err != nil {
		t.Fatal(err)
	}
	ft.mu.Lock()
	sent := ft.sent[0]
	ft.mu.Unlock()
	// ╔ encodes to CP437 0xC9; → is unrepresentable → '?'.
	if string(sent) != string([]byte{0xC9, '?'}) {
		t.Fatalf("sent %v", sent)
	}

	ft2 := &fakeTransport{}
	s2 := newFakeSession(t, ft2, Options{})
	if err := s2.Send(context.Background(), "é"); err != nil {
		t.Fatal(err)
	}
	ft2.mu.Lock()
	sent2 := ft2.sent[0]
	ft2.mu.Unlock()
	if string(sent2) != "é" {
		t.Fatalf("sent %q", sent2)
	}
}

func TestWaitForUpdateTimesOutAndCtx(t *testing.T) {
	ft := &fakeTransport{}
	s := newFakeSession(t, ft, Options{})
	ok, err := s.WaitForUpdate(context.Background(), 20*time.Millisecond)
	if err != nil || ok {
		t.Fatalf("ok=%v err=%v", ok, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := s.WaitForUpdate(ctx, time.Hour); !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v", err)
	}
}

func TestWaitForScreenChange(t *testing.T) {
	ft := &fakeTransport{}
	s := newFakeSession(t, ft, Options{})
	since := s.ScreenChangeSeq()
	// Timeout with no change.
	changed, err := s.WaitForScreenChange(context.Background(), 20*time.Millisecond, since)
	if err != nil || changed {
		t.Fatalf("changed=%v err=%v", changed, err)
	}
	// Change arrives.
	go func() {
		time.Sleep(10 * time.Millisecond)
		ft.queue([]byte("x"))
	}()
	changed, err = s.WaitForScreenChange(context.Background(), 2*time.Second, since)
	if err != nil || !changed {
		t.Fatalf("changed=%v err=%v", changed, err)
	}
	// since already surpassed returns immediately.
	changed, err = s.WaitForScreenChange(context.Background(), time.Hour, since)
	if err != nil || !changed {
		t.Fatalf("changed=%v err=%v", changed, err)
	}
	// Negative since: any next update.
	go func() {
		time.Sleep(10 * time.Millisecond)
		ft.queue([]byte("y"))
	}()
	changed, err = s.WaitForScreenChange(context.Background(), 2*time.Second, -1)
	if err != nil || !changed {
		t.Fatalf("changed=%v err=%v", changed, err)
	}
	// Context cancellation.
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := s.WaitForScreenChange(ctx, time.Hour, s.ScreenChangeSeq()); !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v", err)
	}
}

func TestSendExpect(t *testing.T) {
	ft := &fakeTransport{}
	s := newFakeSession(t, ft, Options{})
	go func() {
		time.Sleep(10 * time.Millisecond)
		ft.queue([]byte("Command [TL]: "))
	}()
	res, err := s.SendExpect(context.Background(), "look", session.ExpectOptions{ExpectText: "Command", Timeout: 2 * time.Second})
	if err != nil || !res.Matched {
		t.Fatalf("res=%+v err=%v", res, err)
	}
	ft.mu.Lock()
	defer ft.mu.Unlock()
	if string(ft.sent[0]) != "look" {
		t.Fatalf("sent %q", ft.sent[0])
	}
}

func TestReceiveErrorDisconnects(t *testing.T) {
	ft := &fakeTransport{}
	s := newFakeSession(t, ft, Options{})
	ft.mu.Lock()
	ft.recvErr = errors.New("connection lost")
	ft.mu.Unlock()
	deadline := time.Now().Add(2 * time.Second)
	for s.IsConnected() {
		if time.Now().After(deadline) {
			t.Fatal("session stayed connected after receive error")
		}
		time.Sleep(2 * time.Millisecond)
	}
	if s.Emulator() == nil {
		t.Fatal("nil emulator")
	}
}

func TestConnectFailurePropagates(t *testing.T) {
	boom := errors.New("dial failed")
	s := New(&fakeTransport{}, func(context.Context) error { return boom }, Options{})
	if err := s.Connect(context.Background()); !errors.Is(err, boom) {
		t.Fatalf("err = %v", err)
	}
	// Close before Connect is safe.
	if err := s.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestSendErrorPropagates(t *testing.T) {
	ft := &fakeTransport{sendErr: errors.New("pipe broken")}
	s := newFakeSession(t, ft, Options{})
	if err := s.Send(context.Background(), "x"); err == nil {
		t.Fatal("expected send error")
	}
}

// readLineFrom accumulates Read calls into buf until a line terminator
// ('\r' or '\n') is seen, the buffer fills, or a read error/EOF occurs, then
// returns the bytes read so far. A single Read call is not guaranteed to
// return a whole line — TCP has no message boundaries — so a test scripting
// a line-oriented exchange over a raw net.Conn must loop, not assume one
// call captures everything.
func readLineFrom(conn net.Conn, buf []byte) []byte {
	total := 0
	for total < len(buf) {
		n, err := conn.Read(buf[total:])
		total += n
		if n > 0 && (buf[total-1] == '\r' || buf[total-1] == '\n') {
			break
		}
		if err != nil {
			break
		}
	}
	return buf[:total]
}

// TestTelnetSessionLoopback runs the real telnet transport against a
// scripted TCP server end-to-end.
func TestTelnetSessionLoopback(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = ln.Close() }()
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		defer func() { _ = conn.Close() }()
		// Ignore the client's negotiation, send a banner with CP437 art. The
		// read deadline is a generous safety net (not protocol timing).
		buf := make([]byte, 64)
		_ = conn.SetReadDeadline(time.Now().Add(30 * time.Second))
		_, _ = conn.Read(buf)
		_, _ = conn.Write(append([]byte{0xC9, 0xCD, 0xBB, '\r', '\n'}, []byte("login: ")...))
		// Echo the next line back. TCP gives no guarantee a single Read call
		// returns the client's whole "guest\r" write in one shot — a short
		// read here would echo a truncated fragment and the client would
		// never see "guest", regardless of how generous any timeout is (this
		// was the actual cause of a flake previously misdiagnosed as CPU
		// contention: widening 10s->30s made zero difference because the
		// bug was never about time). Accumulate until a line terminator.
		_ = conn.SetReadDeadline(time.Now().Add(30 * time.Second))
		line := readLineFrom(conn, buf)
		_, _ = conn.Write(line)
		time.Sleep(100 * time.Millisecond)
	}()

	addr := ln.Addr().(*net.TCPAddr)
	s, err := ConnectTelnet(context.Background(), "127.0.0.1", addr.Port, TelnetOptions{Cols: 40, Rows: 5})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = s.Close(context.Background()) }()

	snap := waitForScreen(t, s, "login:")
	if !strings.Contains(snap.Screen, "╔═╗") {
		t.Fatalf("screen = %q", snap.Screen)
	}
	if err := s.Send(context.Background(), "guest\r"); err != nil {
		t.Fatal(err)
	}
	waitForScreen(t, s, "guest")
}

func TestNewWSSessionDefaults(t *testing.T) {
	s := NewWSSession("ws://127.0.0.1:1/nope", WSOptions{Origin: "http://localhost"})
	// Dial fails fast against a closed port; the constructor wiring is what
	// is under test.
	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()
	if err := s.Connect(ctx); err == nil {
		_ = s.Close(context.Background())
		t.Fatal("expected connect error against closed port")
	}
}

func TestConnectFactoriesAndDefaults(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()
	if _, err := ConnectWS(ctx, "ws://127.0.0.1:1/nope", WSOptions{Cols: 10, Rows: 2}); err == nil {
		t.Fatal("expected ws connect error")
	}
	if _, err := ConnectTelnet(ctx, "127.0.0.1", 1, TelnetOptions{}); err == nil {
		t.Fatal("expected telnet connect error against closed port")
	}
	// Constructors fill 80×25 / ANSI defaults.
	s := NewTelnetSession("127.0.0.1", 1, TelnetOptions{})
	if snap := s.Snapshot(); snap.Cols != 80 || snap.Rows != 25 {
		t.Fatalf("snap = %+v", snap)
	}
	ws := NewWSSession("ws://x", WSOptions{})
	if snap := ws.Snapshot(); snap.Cols != 80 || snap.Rows != 25 {
		t.Fatalf("snap = %+v", snap)
	}
}

func TestWaitForScreenChangeFutureSince(t *testing.T) {
	ft := &fakeTransport{}
	s := newFakeSession(t, ft, Options{})
	// since is ahead of any update this test produces: one update arrives
	// (waking the waiter), the loop re-checks, then the timeout expires.
	future := s.ScreenChangeSeq() + 5
	go func() {
		time.Sleep(5 * time.Millisecond)
		ft.queue([]byte("z"))
	}()
	changed, err := s.WaitForScreenChange(context.Background(), 150*time.Millisecond, future)
	if err != nil || changed {
		t.Fatalf("changed=%v err=%v", changed, err)
	}
}

func TestConnectWSSuccessAgainstEchoServer(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, &websocket.AcceptOptions{OriginPatterns: []string{"*"}})
		if err != nil {
			return
		}
		defer func() { _ = c.CloseNow() }()
		ctx := r.Context()
		_ = c.Write(ctx, websocket.MessageText, []byte("welcome> "))
		for {
			typ, data, err := c.Read(ctx)
			if err != nil {
				return
			}
			if err := c.Write(ctx, typ, data); err != nil {
				return
			}
		}
	}))
	defer srv.Close()
	url := "ws" + strings.TrimPrefix(srv.URL, "http")
	s, err := ConnectWS(context.Background(), url, WSOptions{Cols: 40, Rows: 5})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = s.Close(context.Background()) }()
	waitForScreen(t, s, "welcome>")
	if err := s.Send(context.Background(), "ping"); err != nil {
		t.Fatal(err)
	}
	waitForScreen(t, s, "ping")
}

func TestWaitForScreenChangeZeroTimeout(t *testing.T) {
	ft := &fakeTransport{}
	s := newFakeSession(t, ft, Options{})
	changed, err := s.WaitForScreenChange(context.Background(), 0, s.ScreenChangeSeq()+1)
	if err != nil || changed {
		t.Fatalf("changed=%v err=%v", changed, err)
	}
}
