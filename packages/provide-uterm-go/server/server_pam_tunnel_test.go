//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/pty"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnelclient"
)

// ── test doubles ────────────────────────────────────────────────────────────

// fakeTunnel is an in-memory tunnelConn: Recv drains recvCh (or unblocks on
// Close), SendData records payloads.
type fakeTunnel struct {
	connectErr error
	openErr    error
	sendErr    error
	sendBlock  chan struct{} // when non-nil, SendData parks until ctx/close/this

	sendCalls atomic.Int32

	mu      sync.Mutex
	sent    [][]byte
	opened  bool
	closed  bool
	recvCh  chan tunnelclient.Frame
	closeCh chan struct{}
}

func newFakeTunnel() *fakeTunnel {
	return &fakeTunnel{recvCh: make(chan tunnelclient.Frame, 8), closeCh: make(chan struct{})}
}

func (f *fakeTunnel) Connect(context.Context) error { return f.connectErr }

func (f *fakeTunnel) OpenTerminal(_ context.Context, _, _ int) error {
	f.mu.Lock()
	f.opened = true
	f.mu.Unlock()
	return f.openErr
}

func (f *fakeTunnel) SendData(ctx context.Context, data []byte, _ byte) error {
	f.sendCalls.Add(1)
	if f.sendBlock != nil {
		select {
		case <-f.sendBlock:
		case <-f.closeCh:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	if f.sendErr != nil {
		return f.sendErr
	}
	f.mu.Lock()
	f.sent = append(f.sent, append([]byte(nil), data...))
	f.mu.Unlock()
	return nil
}

func (f *fakeTunnel) Recv(ctx context.Context) (tunnelclient.Frame, error) {
	select {
	case fr := <-f.recvCh:
		return fr, nil
	case <-f.closeCh:
		return tunnelclient.Frame{}, io.EOF
	case <-ctx.Done():
		return tunnelclient.Frame{}, ctx.Err()
	}
}

func (f *fakeTunnel) Close() error {
	f.mu.Lock()
	already := f.closed
	f.closed = true
	f.mu.Unlock()
	if !already {
		close(f.closeCh)
	}
	return nil
}

func (f *fakeTunnel) sentCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.sent)
}

func (f *fakeTunnel) isClosed() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.closed
}

// fakePTYConnector satisfies ptyBridgeConnector: Read serves output, Write
// records inbound bytes.
type fakePTYConnector struct {
	out      io.Reader
	writeErr error

	writeCalls atomic.Int32
	mu         sync.Mutex
	writes     []byte
}

func (c *fakePTYConnector) Read(p []byte) (int, error) { return c.out.Read(p) }

func (c *fakePTYConnector) Write(p []byte) (int, error) {
	c.writeCalls.Add(1)
	if c.writeErr != nil {
		return 0, c.writeErr
	}
	c.mu.Lock()
	c.writes = append(c.writes, p...)
	c.mu.Unlock()
	return len(p), nil
}

func (c *fakePTYConnector) written() []byte {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]byte(nil), c.writes...)
}

// eofReader returns EOF on the first Read, signalling done. Used to drive the
// outbound PTY reader to close its data channel.
type eofReader struct {
	done chan struct{}
	once sync.Once
}

func (r *eofReader) Read([]byte) (int, error) {
	r.once.Do(func() { close(r.done) })
	return 0, io.EOF
}

// fakeCaptureConnector satisfies captureBridgeConnector.
type fakeCaptureConnector struct {
	frames  chan pty.CaptureFrame
	closeCh chan struct{}
}

func newFakeCaptureConnector() *fakeCaptureConnector {
	return &fakeCaptureConnector{frames: make(chan pty.CaptureFrame, 8), closeCh: make(chan struct{})}
}

func (c *fakeCaptureConnector) ReadFrame(ctx context.Context) (pty.CaptureFrame, error) {
	select {
	case fr := <-c.frames:
		return fr, nil
	case <-c.closeCh:
		return pty.CaptureFrame{}, io.EOF
	case <-ctx.Done():
		return pty.CaptureFrame{}, ctx.Err()
	}
}

// fakeConnectorRegistry adds the optional connectorLookup surface to fakeRegistry.
type fakeConnectorRegistry struct {
	*fakeRegistry
	connector any
}

func (r *fakeConnectorRegistry) GetConnector(_ context.Context, _ string) (any, bool) {
	if r.connector == nil {
		return nil, false
	}
	return r.connector, true
}

// waitFor polls cond until true or the deadline, failing the test on timeout.
func waitFor(t *testing.T, cond func() bool, msg string) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", msg)
}

// ── createRelayTunnel ───────────────────────────────────────────────────────

func TestPamCreateRelayTunnelSuccess(t *testing.T) {
	type req struct {
		path, auth, ctype string
		body              map[string]any
	}
	got := make(chan req, 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var b map[string]any
		_ = json.NewDecoder(r.Body).Decode(&b)
		got <- req{path: r.URL.Path, auth: r.Header.Get("Authorization"), ctype: r.Header.Get("Content-Type"), body: b}
		_ = json.NewEncoder(w).Encode(map[string]string{
			"worker_token": "wtok", "ws_endpoint": "wss://tunnel.example/ws",
		})
	}))
	defer srv.Close()

	pi := newPamIntegration(serverconfig.PamConfig{RelayURL: strptr(srv.URL), RelayToken: strptr("rtok")}, newFakeRegistry())
	token, endpoint, err := pi.createRelayTunnel(context.Background(), "pam-a-1", "a (pts/1)")
	if err != nil {
		t.Fatalf("createRelayTunnel: %v", err)
	}
	if token != "wtok" || endpoint != "wss://tunnel.example/ws" {
		t.Fatalf("parsed wrong: token=%q endpoint=%q", token, endpoint)
	}
	select {
	case c := <-got:
		if c.path != "/api/tunnels" || c.auth != "Bearer rtok" || c.ctype != "application/json" {
			t.Fatalf("unexpected request: %+v", c)
		}
		if c.body["session_id"] != "pam-a-1" || c.body["display_name"] != "a (pts/1)" || c.body["tunnel_type"] != "terminal" {
			t.Fatalf("unexpected body: %+v", c.body)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("relay never received the tunnels POST")
	}
}

func TestPamCreateRelayTunnelStatusError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()
	pi := newPamIntegration(serverconfig.PamConfig{RelayURL: strptr(srv.URL), RelayToken: strptr("t")}, newFakeRegistry())
	if _, _, err := pi.createRelayTunnel(context.Background(), "s", "d"); err == nil {
		t.Fatal("want error on 500 response")
	}
}

func TestPamCreateRelayTunnelBadJSON(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("not json"))
	}))
	defer srv.Close()
	pi := newPamIntegration(serverconfig.PamConfig{RelayURL: strptr(srv.URL), RelayToken: strptr("t")}, newFakeRegistry())
	if _, _, err := pi.createRelayTunnel(context.Background(), "s", "d"); err == nil {
		t.Fatal("want error on unparseable body")
	}
}

func TestPamCreateRelayTunnelEgressBlocked(t *testing.T) {
	pi := newPamIntegration(serverconfig.PamConfig{
		RelayURL: strptr("http://169.254.169.254"), RelayToken: strptr("t"),
	}, newFakeRegistry())
	if _, _, err := pi.createRelayTunnel(context.Background(), "s", "d"); err == nil {
		t.Fatal("want error when the target is egress-blocked")
	}
}

func TestPamCreateRelayTunnelPostError(t *testing.T) {
	pi := newPamIntegration(serverconfig.PamConfig{
		RelayURL: strptr("http://127.0.0.1:1"), RelayToken: strptr("t"),
	}, newFakeRegistry())
	if _, _, err := pi.createRelayTunnel(context.Background(), "s", "d"); err == nil {
		t.Fatal("want error when the POST is refused")
	}
}

// ── bridge lifecycle (direct) ───────────────────────────────────────────────

func TestPamTunnelBridgePTYPump(t *testing.T) {
	pr, pw := io.Pipe()
	t.Cleanup(func() { _ = pw.Close() })
	conn := &fakePTYConnector{out: pr}
	ft := newFakeTunnel()

	b := NewPamTunnelBridge("wss://x", "tok", conn, quietLogger())
	b.newTunnel = func(string, string) tunnelConn { return ft }
	if err := b.Start(context.Background()); err != nil {
		t.Fatalf("Start: %v", err)
	}

	// Outbound: PTY output → tunnel.
	if _, err := pw.Write([]byte("hello")); err != nil {
		t.Fatalf("pipe write: %v", err)
	}
	waitFor(t, func() bool { return ft.sentCount() >= 1 }, "outbound PTY data")
	if string(ft.sent[0]) != "hello" {
		t.Fatalf("outbound payload = %q, want hello", ft.sent[0])
	}

	// Inbound: CHANNEL_DATA tunnel frame → PTY write.
	ft.recvCh <- tunnelclient.Frame{Channel: tunnelclient.ChannelData, Payload: []byte("world")}
	waitFor(t, func() bool { return string(conn.written()) == "world" }, "inbound PTY write")

	// Non-data channel and empty payload frames are ignored.
	ft.recvCh <- tunnelclient.Frame{Channel: tunnelclient.ChannelControl, Payload: []byte("skip")}
	ft.recvCh <- tunnelclient.Frame{Channel: tunnelclient.ChannelData, Payload: nil}

	b.Stop()
	if !ft.isClosed() {
		t.Fatal("Stop must close the tunnel")
	}
	if string(conn.written()) != "world" {
		t.Fatalf("inbound writes = %q, want world (filtered frames dropped)", conn.written())
	}
}

func TestPamTunnelBridgePTYInboundEOF(t *testing.T) {
	pr, pw := io.Pipe()
	t.Cleanup(func() { _ = pw.Close() })
	conn := &fakePTYConnector{out: pr}
	ft := newFakeTunnel()
	b := NewPamTunnelBridge("wss://x", "tok", conn, quietLogger())
	b.newTunnel = func(string, string) tunnelConn { return ft }
	if err := b.Start(context.Background()); err != nil {
		t.Fatalf("Start: %v", err)
	}
	// An EOF frame ends the inbound loop; wait for it to be consumed (so the
	// IsEOF branch runs) before Stop, which must still complete cleanly.
	ft.recvCh <- tunnelclient.Frame{Channel: tunnelclient.ChannelData, Flags: tunnelclient.FlagEOF}
	waitFor(t, func() bool { return len(ft.recvCh) == 0 }, "EOF frame consumed")
	b.Stop()
}

func TestPamTunnelBridgePTYOutputEOF(t *testing.T) {
	// The PTY output reader hits EOF → closes the outbound data channel → the
	// send loop returns via the closed-channel branch (ctx still live, so it is
	// the only reachable exit).
	r := &eofReader{done: make(chan struct{})}
	conn := &fakePTYConnector{out: r}
	ft := newFakeTunnel()
	b := NewPamTunnelBridge("wss://x", "tok", conn, quietLogger())
	b.newTunnel = func(string, string) tunnelConn { return ft }
	if err := b.Start(context.Background()); err != nil {
		t.Fatalf("Start: %v", err)
	}
	<-r.done                          // reader observed EOF and closed the data channel
	time.Sleep(20 * time.Millisecond) // let the send loop drain the closed channel
	b.Stop()
}

func TestPamTunnelBridgePTYWriteError(t *testing.T) {
	pr, pw := io.Pipe()
	t.Cleanup(func() { _ = pw.Close() })
	conn := &fakePTYConnector{out: pr, writeErr: errFixed("write boom")}
	ft := newFakeTunnel()
	b := NewPamTunnelBridge("wss://x", "tok", conn, quietLogger())
	b.newTunnel = func(string, string) tunnelConn { return ft }
	if err := b.Start(context.Background()); err != nil {
		t.Fatalf("Start: %v", err)
	}
	ft.recvCh <- tunnelclient.Frame{Channel: tunnelclient.ChannelData, Payload: []byte("x")}
	waitFor(t, func() bool { return conn.writeCalls.Load() >= 1 }, "inbound write attempted")
	b.Stop() // inbound loop returns after the write error; no hang
}

// repeatReader endlessly yields single 'x' bytes (never EOFs), to keep the
// outbound reader producing chunks.
type repeatReader struct{}

func (repeatReader) Read(p []byte) (int, error) {
	if len(p) == 0 {
		return 0, nil
	}
	p[0] = 'x'
	return 1, nil
}

func TestPamTunnelBridgePTYReaderCancel(t *testing.T) {
	// With SendData parked, the outbound reader fills the unbuffered channel and
	// blocks on the send; Stop cancels the pump ctx, and the reader exits via its
	// ctx.Done branch (the shutdown-race path).
	conn := &fakePTYConnector{out: repeatReader{}}
	ft := newFakeTunnel()
	ft.sendBlock = make(chan struct{}) // never closed → SendData parks on ctx
	b := NewPamTunnelBridge("wss://x", "tok", conn, quietLogger())
	b.newTunnel = func(string, string) tunnelConn { return ft }
	if err := b.Start(context.Background()); err != nil {
		t.Fatalf("Start: %v", err)
	}
	waitFor(t, func() bool { return ft.sendCalls.Load() >= 1 }, "first send parked")
	b.Stop() // cancels ctx: parked SendData returns, reader exits via ctx.Done
}

func TestPamTunnelBridgePTYSendError(t *testing.T) {
	pr, pw := io.Pipe()
	t.Cleanup(func() { _ = pw.Close() })
	conn := &fakePTYConnector{out: pr}
	ft := newFakeTunnel()
	ft.sendErr = errFixed("send boom")
	b := NewPamTunnelBridge("wss://x", "tok", conn, quietLogger())
	b.newTunnel = func(string, string) tunnelConn { return ft }
	if err := b.Start(context.Background()); err != nil {
		t.Fatalf("Start: %v", err)
	}
	// Output triggers a send that errors → the outbound loop returns.
	if _, err := pw.Write([]byte("boom")); err != nil {
		t.Fatalf("pipe write: %v", err)
	}
	waitFor(t, func() bool { return ft.sendCalls.Load() >= 1 }, "outbound send attempted")
	b.Stop()
}

func TestPamTunnelBridgeCapturePump(t *testing.T) {
	conn := newFakeCaptureConnector()
	t.Cleanup(func() { close(conn.closeCh) })
	ft := newFakeTunnel()
	b := NewPamTunnelBridge("wss://x", "tok", conn, quietLogger())
	b.newTunnel = func(string, string) tunnelConn { return ft }
	if err := b.Start(context.Background()); err != nil {
		t.Fatalf("Start: %v", err)
	}
	// stdin frames are ignored; stdout frames are forwarded.
	conn.frames <- pty.CaptureFrame{Channel: pty.ChannelStdin, Data: []byte("ignored")}
	conn.frames <- pty.CaptureFrame{Channel: pty.ChannelStdout, Data: []byte("visible")}
	waitFor(t, func() bool { return ft.sentCount() >= 1 }, "capture stdout forwarded")
	if string(ft.sent[0]) != "visible" {
		t.Fatalf("forwarded %q, want visible", ft.sent[0])
	}
	b.Stop()
	if !ft.isClosed() {
		t.Fatal("Stop must close the tunnel")
	}
}

func TestPamTunnelBridgeCaptureSendError(t *testing.T) {
	conn := newFakeCaptureConnector()
	t.Cleanup(func() { close(conn.closeCh) })
	ft := newFakeTunnel()
	ft.sendErr = errFixed("send boom")
	b := NewPamTunnelBridge("wss://x", "tok", conn, quietLogger())
	b.newTunnel = func(string, string) tunnelConn { return ft }
	if err := b.Start(context.Background()); err != nil {
		t.Fatalf("Start: %v", err)
	}
	conn.frames <- pty.CaptureFrame{Channel: pty.ChannelStdout, Data: []byte("x")}
	waitFor(t, func() bool { return ft.sendCalls.Load() >= 1 }, "capture send attempted")
	b.Stop() // capture loop returns after send error
}

func TestPamTunnelBridgeConnectError(t *testing.T) {
	ft := newFakeTunnel()
	ft.connectErr = errFixed("dial boom")
	b := NewPamTunnelBridge("wss://x", "tok", &fakePTYConnector{out: io.MultiReader()}, quietLogger())
	b.newTunnel = func(string, string) tunnelConn { return ft }
	if err := b.Start(context.Background()); err == nil {
		t.Fatal("Start must fail when Connect errors")
	}
	b.Stop() // safe even though start failed
}

func TestPamTunnelBridgeOpenError(t *testing.T) {
	ft := newFakeTunnel()
	ft.openErr = errFixed("open boom")
	b := NewPamTunnelBridge("wss://x", "tok", &fakePTYConnector{out: io.MultiReader()}, quietLogger())
	b.newTunnel = func(string, string) tunnelConn { return ft }
	if err := b.Start(context.Background()); err == nil {
		t.Fatal("Start must fail when OpenTerminal errors")
	}
	if !ft.isClosed() {
		t.Fatal("Start must close the tunnel after an OpenTerminal failure")
	}
}

func TestPamTunnelBridgeUnpumpableConnector(t *testing.T) {
	ft := newFakeTunnel()
	b := NewPamTunnelBridge("wss://x", "tok", struct{}{}, quietLogger())
	b.newTunnel = func(string, string) tunnelConn { return ft }
	if err := b.Start(context.Background()); err != nil {
		t.Fatalf("Start should succeed with no pump: %v", err)
	}
	b.Stop()
}

func TestPamTunnelBridgeStopNeverStarted(t *testing.T) {
	b := NewPamTunnelBridge("wss://x", "tok", struct{}{}, quietLogger())
	b.Stop() // no cancel, no tunnel — must be a clean no-op
}

func TestPamTunnelBridgeDefaultFactory(t *testing.T) {
	// A freshly constructed bridge wires the real tunnel client factory.
	b := NewPamTunnelBridge("wss://x", "tok", struct{}{}, quietLogger())
	if b.newTunnel == nil {
		t.Fatal("default tunnel factory must be set")
	}
	if tc := b.newTunnel("wss://x", "tok"); tc == nil {
		t.Fatal("default factory must build a client")
	}
}

// ── provisionTunnel / onOpen-onClose wiring ─────────────────────────────────

// relayTunnelServer returns an httptest server that answers pam-events with 200
// and tunnels with a valid provisioning body.
func relayTunnelServer(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/tunnels" {
			_ = json.NewEncoder(w).Encode(map[string]string{"worker_token": "wtok", "ws_endpoint": "wss://t/ws"})
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestPamProvisionTunnelStartsAndStopsBridge(t *testing.T) {
	srv := relayTunnelServer(t)
	conn := newFakeCaptureConnector()
	reg := &fakeConnectorRegistry{fakeRegistry: newFakeRegistry(), connector: conn}
	ft := newFakeTunnel()

	pi := NewPamIntegration(serverconfig.PamConfig{
		Mode: "notify", RelayURL: strptr(srv.URL), RelayToken: strptr("t"),
	}, reg, nil, quietLogger())
	pi.newTunnel = func(string, string) tunnelConn { return ft }

	ev := pty.PamEvent{Event: "open", Username: "a", TTY: "/dev/pts/1"}
	pi.onOpen(context.Background(), ev)

	pi.bridgesMu.Lock()
	_, ok := pi.bridges["pam-a-1"]
	pi.bridgesMu.Unlock()
	if !ok {
		t.Fatal("onOpen must store a bridge for the session")
	}
	waitFor(t, func() bool {
		f := ft
		return f.opened
	}, "tunnel opened")

	pi.onClose(context.Background(), pty.PamEvent{Event: "close", Username: "a", TTY: "/dev/pts/1"})
	pi.bridgesMu.Lock()
	_, stillThere := pi.bridges["pam-a-1"]
	pi.bridgesMu.Unlock()
	if stillThere {
		t.Fatal("onClose must remove the bridge")
	}
	if !ft.isClosed() {
		t.Fatal("onClose must stop the bridge (closing the tunnel)")
	}
}

func TestPamProvisionTunnelNoConnector(t *testing.T) {
	srv := relayTunnelServer(t)
	// fakeConnectorRegistry with a nil connector → GetConnector returns (nil,false).
	reg := &fakeConnectorRegistry{fakeRegistry: newFakeRegistry()}
	pi := NewPamIntegration(serverconfig.PamConfig{
		Mode: "notify", RelayURL: strptr(srv.URL), RelayToken: strptr("t"),
	}, reg, nil, quietLogger())
	pi.newTunnel = func(string, string) tunnelConn { return newFakeTunnel() }
	pi.provisionTunnel(context.Background(), pty.PamEvent{Username: "a", TTY: "/dev/pts/1"})
	pi.bridgesMu.Lock()
	n := len(pi.bridges)
	pi.bridgesMu.Unlock()
	if n != 0 {
		t.Fatalf("no connector → no bridge, got %d", n)
	}
}

func TestPamProvisionTunnelRegistryWithoutLookup(t *testing.T) {
	srv := relayTunnelServer(t)
	// Plain fakeRegistry does NOT implement connectorLookup.
	pi := NewPamIntegration(serverconfig.PamConfig{
		Mode: "notify", RelayURL: strptr(srv.URL), RelayToken: strptr("t"),
	}, newFakeRegistry(), nil, quietLogger())
	pi.newTunnel = func(string, string) tunnelConn { return newFakeTunnel() }
	pi.provisionTunnel(context.Background(), pty.PamEvent{Username: "a", TTY: "/dev/pts/1"})
	pi.bridgesMu.Lock()
	n := len(pi.bridges)
	pi.bridgesMu.Unlock()
	if n != 0 {
		t.Fatalf("registry without connector lookup → no bridge, got %d", n)
	}
}

func TestPamProvisionTunnelRelayFails(t *testing.T) {
	// Relay returns 500 for tunnels → createRelayTunnel errors → no bridge, and
	// the connector is never consulted.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/tunnels" {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	reg := &fakeConnectorRegistry{fakeRegistry: newFakeRegistry(), connector: newFakeCaptureConnector()}
	pi := NewPamIntegration(serverconfig.PamConfig{
		Mode: "notify", RelayURL: strptr(srv.URL), RelayToken: strptr("t"),
	}, reg, nil, quietLogger())
	pi.newTunnel = func(string, string) tunnelConn { return newFakeTunnel() }
	pi.provisionTunnel(context.Background(), pty.PamEvent{Username: "a", TTY: "/dev/pts/1"})
	pi.bridgesMu.Lock()
	n := len(pi.bridges)
	pi.bridgesMu.Unlock()
	if n != 0 {
		t.Fatalf("relay failure → no bridge, got %d", n)
	}
}

func TestPamProvisionTunnelStartFailure(t *testing.T) {
	srv := relayTunnelServer(t)
	reg := &fakeConnectorRegistry{fakeRegistry: newFakeRegistry(), connector: newFakeCaptureConnector()}
	ft := newFakeTunnel()
	ft.connectErr = errFixed("dial boom") // Start fails
	pi := NewPamIntegration(serverconfig.PamConfig{
		Mode: "notify", RelayURL: strptr(srv.URL), RelayToken: strptr("t"),
	}, reg, nil, quietLogger())
	pi.newTunnel = func(string, string) tunnelConn { return ft }
	pi.provisionTunnel(context.Background(), pty.PamEvent{Username: "a", TTY: "/dev/pts/1"})
	pi.bridgesMu.Lock()
	n := len(pi.bridges)
	pi.bridgesMu.Unlock()
	if n != 0 {
		t.Fatalf("start failure → bridge not stored, got %d", n)
	}
}

func TestPamStopBridgeUnknownSession(t *testing.T) {
	pi := newPamIntegration(serverconfig.PamConfig{Mode: "notify"}, newFakeRegistry())
	pi.stopBridge("nope") // no bridge for the id — clean no-op
}
