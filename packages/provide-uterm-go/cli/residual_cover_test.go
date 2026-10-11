//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"io"
	"math"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/creack/pty"
	xterm "golang.org/x/term"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/session"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/transports"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnelclient"
)

var errResidual = errors.New("injected failure")

// unwritableTokenPath points the dev-token path below a regular file, so the
// directory cannot be created whoever runs the test (root included).
func unwritableTokenPath(t *testing.T) {
	t.Helper()
	blocker := filepath.Join(t.TempDir(), "blocker")
	if err := os.WriteFile(blocker, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("UTERM_DEV_TOKEN_PATH", filepath.Join(blocker, "dev_token"))
}

// A dev_token server whose token cannot be written must not start: the
// operator would otherwise get a server nobody can authenticate to.
func TestLiveServerDevTokenWriteFailureStopsTheBuild(t *testing.T) {
	unwritableTokenPath(t)
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = ln.Close() }()
	srv, err := NewLiveServer(context.Background(), ln, LiveServerOptions{AuthMode: "dev_token"})
	if err == nil || srv != nil || !strings.Contains(err.Error(), "not a directory") {
		t.Fatalf("NewLiveServer = %v, %v; want a dev-token write failure", srv, err)
	}
}

func TestBuildServerFailsOnEachConstructionStep(t *testing.T) {
	t.Run("telemetry", func(t *testing.T) {
		_, err := buildServerFromConfig(context.Background(), buildFailureConfig(), "",
			func(s *buildSeams) { s.setupTelemetry = func() error { return errResidual } })
		if !errors.Is(err, errResidual) {
			t.Fatalf("err = %v", err)
		}
	})
	t.Run("unsupported control-plane backend", func(t *testing.T) {
		cfg := buildFailureConfig()
		cfg.ControlPlane.Backend = "postgres"
		_, err := buildServerFromConfig(context.Background(), cfg, "")
		if err == nil || !strings.Contains(err.Error(), "unsupported control-plane backend") {
			t.Fatalf("err = %v", err)
		}
	})
	t.Run("migration", func(t *testing.T) {
		// A schema-version table without a version column: the database opens,
		// but the migration's version scan cannot run against it.
		path := filepath.Join(t.TempDir(), "cp.db")
		db, err := sql.Open("sqlite", path)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := db.Exec("CREATE TABLE cp_schema_version (x TEXT)"); err != nil {
			t.Fatal(err)
		}
		_ = db.Close()
		cfg := buildFailureConfig()
		cfg.ControlPlane.Backend = "sqlite"
		cfg.ControlPlane.DatabaseURL = &path
		bundle, err := buildServerFromConfig(context.Background(), cfg, "")
		if err == nil || bundle != nil {
			t.Fatalf("build = %v, %v; want a migration failure", bundle, err)
		}
	})
	t.Run("duplicate graphical target", func(t *testing.T) {
		cfg := buildFailureConfig()
		target := serverconfig.GraphicalTargetConfig{
			TargetID: "gt-dup", TenantID: "acme", Protocol: "litevirt", TargetAddress: "vm.local:9000",
			Enabled: true, Config: map[string]any{"vm_name": "v"},
		}
		cfg.GraphicalTargets = []serverconfig.GraphicalTargetConfig{target, target}
		bundle, err := buildServerFromConfig(context.Background(), cfg, "")
		if err == nil || bundle != nil {
			t.Fatalf("build = %v, %v; want the duplicate static target refused", bundle, err)
		}
	})
	t.Run("server construction", func(t *testing.T) {
		var got server.Deps
		bundle, err := buildServerFromConfig(context.Background(), buildFailureConfig(), "",
			func(s *buildSeams) {
				s.newServer = func(d server.Deps) (*server.Server, error) {
					got = d
					return nil, errResidual
				}
			})
		if !errors.Is(err, errResidual) || bundle != nil {
			t.Fatalf("build = %v, %v", bundle, err)
		}
		if got.Hub == nil || got.Registry == nil || got.Auth == nil {
			t.Fatalf("server.New was not handed the wired dependencies: %+v", got)
		}
	})
}

func TestShareDisplayNameFallsBackToLocalhost(t *testing.T) {
	orig := osHostname
	osHostname = func() (string, error) { return "", errResidual }
	t.Cleanup(func() { osHostname = orig })
	if got := shareDisplayName(""); !strings.HasSuffix(got, "@localhost") {
		t.Fatalf("shareDisplayName = %q, want …@localhost", got)
	}
}

// writeFailSource is a PTY source whose writes fail; its reads block until
// then and report EOF afterwards, as a PTY does once its child is gone.
type writeFailSource struct {
	once    sync.Once
	gone    chan struct{}
	mu      sync.Mutex
	written [][]byte
}

func newWriteFailSource() *writeFailSource { return &writeFailSource{gone: make(chan struct{})} }

func (s *writeFailSource) Read([]byte) (int, error) {
	<-s.gone
	return 0, io.EOF
}

func (s *writeFailSource) Write(p []byte) (int, error) {
	s.mu.Lock()
	s.written = append(s.written, append([]byte(nil), p...))
	s.mu.Unlock()
	s.once.Do(func() { close(s.gone) })
	return 0, errResidual
}

func (s *writeFailSource) Close() error { return nil }

// A PTY that refuses inbound bytes ends the bridge instead of spinning.
func TestBridgeShareEndsWhenThePTYRefusesInput(t *testing.T) {
	f := newFakeTunnelServer(t, func(ctx context.Context, c *websocket.Conn) {
		_ = c.Write(ctx, websocket.MessageBinary, []byte("typed"))
		for {
			if _, _, err := c.Read(ctx); err != nil {
				return
			}
		}
	})
	client := tunnelclient.NewClient("ws"+strings.TrimPrefix(f.srv.URL, "http")+"/tunnel", "")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := client.Connect(ctx); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = client.Close() }()
	src := newWriteFailSource()
	bridgeShare(ctx, client, src)
	if ctx.Err() != nil {
		t.Fatal("bridge only ended on the test timeout")
	}
	if len(src.written) != 1 || string(src.written[0]) != "typed" {
		t.Fatalf("PTY writes = %q", src.written)
	}
}

func TestRunInspectAndTunnelReportChannelOpenFailure(t *testing.T) {
	f := newFakeTunnelServer(t, func(ctx context.Context, c *websocket.Conn) {
		for {
			if _, _, err := c.Read(ctx); err != nil {
				return
			}
		}
	})
	orig := openTunnelChannel
	openTunnelChannel = func(context.Context, *tunnelclient.Client, []byte) error { return errResidual }
	t.Cleanup(func() { openTunnelChannel = orig })

	err := runInspect(context.Background(), inspectOptions{Server: f.srv.URL, Port: 9}, &bytes.Buffer{}, &bytes.Buffer{})
	if !errors.Is(err, errResidual) || !strings.Contains(err.Error(), "cannot open http channel") {
		t.Fatalf("runInspect = %v", err)
	}
	err = runTunnel(context.Background(), tunnelOptions{Server: f.srv.URL, Port: 9}, &bytes.Buffer{})
	if !errors.Is(err, errResidual) || !strings.Contains(err.Error(), "cannot open tcp channel") {
		t.Fatalf("runTunnel = %v", err)
	}
}

// sendHTTP drops an event JSON cannot encode (a NaN intercept timeout parses
// from the flag) instead of sending a half-built frame: with no client wired,
// reaching the send at all would panic.
func TestInspectSendHTTPDropsUnencodableEvent(t *testing.T) {
	s := &inspectSession{}
	s.sendHTTP(context.Background(), map[string]any{"timeout_s": math.NaN()})
}

func TestActionRIDFractionalNumber(t *testing.T) {
	if got := actionRID(1.5); got != "1.5" {
		t.Fatalf("actionRID(1.5) = %q", got)
	}
}

// The upstream's content/transfer encodings describe its own framing, not the
// body relayed here (already decoded and buffered), so they are not copied.
func TestInspectForwardStripsUpstreamEncodingHeaders(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Encoding", "x-custom")
		w.Header().Set("X-Kept", "yes")
		_, _ = w.Write([]byte("body"))
	}))
	defer upstream.Close()
	s := &inspectSession{
		client: tunnelclient.NewClient("ws://127.0.0.1:1/tunnel", ""),
		gate:   tunnelclient.NewInterceptGate(30, "forward"),
		errw:   io.Discard,
	}
	rec := httptest.NewRecorder()
	s.forward(context.Background(), rec, forwardReq{method: http.MethodGet, url: upstream.URL + "/", path: "/", rid: "r1"})
	if rec.Code != http.StatusOK || rec.Body.String() != "body" {
		t.Fatalf("relayed %d %q", rec.Code, rec.Body.String())
	}
	if rec.Header().Get("Content-Encoding") != "" || rec.Header().Get("X-Kept") != "yes" {
		t.Fatalf("relayed headers = %v", rec.Header())
	}
}

func TestCreateTunnelRequestAndResponseErrors(t *testing.T) {
	if _, err := createTunnel(context.Background(), "http://\x7fbad", nil, "", "ua"); err == nil ||
		!strings.Contains(err.Error(), "cannot reach server") {
		t.Fatalf("bad URL: %v", err)
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("not json"))
	}))
	defer srv.Close()
	if _, err := createTunnel(context.Background(), srv.URL, nil, "", "ua"); err == nil ||
		!strings.Contains(err.Error(), "invalid tunnel response") {
		t.Fatalf("bad body: %v", err)
	}
}

// readerConn is a net.Conn whose reads always have data and whose writes
// are recorded: enough for handleTunnelConn's local side.
type readerConn struct {
	net.Conn
	closed chan struct{}
	once   sync.Once
}

func (c *readerConn) Read(p []byte) (int, error) { return copy(p, "x"), nil }
func (c *readerConn) Write(p []byte) (int, error) {
	return len(p), nil
}

func (c *readerConn) Close() error {
	c.once.Do(func() { close(c.closed) })
	return nil
}

// With the tunnel gone, local bytes cannot be forwarded: the relay ends and
// closes the local connection rather than reading on.
func TestHandleTunnelConnEndsWhenForwardingFails(t *testing.T) {
	conn := &readerConn{closed: make(chan struct{})}
	handleTunnelConn(context.Background(), conn, tunnelclient.NewClient("ws://127.0.0.1:1/tunnel", ""))
	select {
	case <-conn.closed:
	default:
		t.Fatal("the local connection was left open")
	}
}

func TestWatchHeaderCarriesBearerToken(t *testing.T) {
	if got := watchHeader("tok", "").Get("Authorization"); got != "Bearer tok" {
		t.Fatalf("Authorization = %q", got)
	}
	if got := watchHeader("", "").Get("Authorization"); got != "" {
		t.Fatalf("no token must send no Authorization, got %q", got)
	}
}

func TestDetailLinesIncludesRequestBody(t *testing.T) {
	lines := detailLines(&exchange{method: "POST", url: "/x", reqBodyB64: "aGVsbG8="})
	joined := strings.Join(lines, "\n")
	if !strings.Contains(joined, "Request Body\nhello") {
		t.Fatalf("detail = %q", joined)
	}
}

// nanSnapshotConnector reports a snapshot JSON cannot encode.
type nanSnapshotConnector struct{ *fakeConnector }

func (nanSnapshotConnector) Snapshot() session.Snapshot {
	return session.Snapshot{CapturedAt: math.NaN()}
}

func TestLastSnapshotReportsUnencodableSnapshot(t *testing.T) {
	r := registryWithConn(t, nanSnapshotConnector{newFakeConnector()})
	snap, err := r.LastSnapshot(context.Background(), "provide-shell")
	if err == nil || snap != nil {
		t.Fatalf("LastSnapshot = %v, %v; want an encode error", snap, err)
	}
}

// A watch whose clock reads past the deadline reports a timeout without
// waiting, even with the bus live.
func TestWatchSessionEventsDeadlineAlreadyPassed(t *testing.T) {
	r := registryWithConn(t, newFakeConnector())
	r.SetEventBus(hub.NewEventBus(hub.EventBusOptions{}))
	orig := watchUntil
	watchUntil = func(time.Time) time.Duration { return 0 }
	t.Cleanup(func() { watchUntil = orig })
	res, err := r.WatchSessionEvents(context.Background(), "provide-shell", server.WatchParams{})
	if err != nil || res["timed_out"] != true {
		t.Fatalf("watch = %v, %v; want timed_out", res, err)
	}
}

// proxyBridgeServer runs bridgeProxy for each WebSocket client over tr.
func proxyBridgeServer(t *testing.T, tr transports.ConnectionTransport, accepted chan<- *websocket.Conn) string {
	t.Helper()
	done := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer conn.CloseNow() //nolint:errcheck // test cleanup
		if accepted != nil {
			accepted <- conn
		}
		ctx, cancel := context.WithCancel(r.Context())
		defer cancel()
		bridgeProxy(ctx, cancel, conn, tr)
		_ = conn.Close(websocket.StatusNormalClosure, "")
		close(done)
	}))
	t.Cleanup(srv.Close)
	t.Cleanup(func() {
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Error("bridge did not finish")
		}
	})
	return "ws" + strings.TrimPrefix(srv.URL, "http")
}

// sendFailTransport records sends and fails every one after the first.
type sendFailTransport struct {
	*fakeTransport
	failAfter int
}

func (s *sendFailTransport) Send(ctx context.Context, data []byte) error {
	s.mu.Lock()
	n := len(s.sent)
	s.mu.Unlock()
	if n >= s.failAfter {
		return errResidual
	}
	return s.fakeTransport.Send(ctx, data)
}

// Empty browser frames are skipped, and a remote that refuses input ends the
// bridge (the browser sees the socket close).
func TestBridgeProxySkipsEmptyFramesAndStopsOnSendFailure(t *testing.T) {
	tr := &sendFailTransport{fakeTransport: newFakeTransport(), failAfter: 1}
	url := proxyBridgeServer(t, tr, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	c, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer c.CloseNow() //nolint:errcheck // test cleanup
	for _, msg := range []string{"", "one", "two"} {
		if err := c.Write(ctx, websocket.MessageText, []byte(msg)); err != nil {
			t.Fatal(err)
		}
	}
	if _, _, err := c.Read(ctx); websocket.CloseStatus(err) != websocket.StatusNormalClosure {
		t.Fatalf("browser read = %v, want the bridge's normal close", err)
	}
	if got := tr.sentStrings(); len(got) != 1 || got[0] != "one" {
		t.Fatalf("sent = %q, want only the first non-empty frame", got)
	}
}

// closingTransport hands back one chunk of output, but first closes the
// browser socket, so relaying that chunk is a write to a closed connection.
type closingTransport struct {
	*fakeTransport
	conn chan *websocket.Conn
	once sync.Once
}

func (c *closingTransport) Receive(ctx context.Context, n int, timeout time.Duration) ([]byte, error) {
	var out []byte
	c.once.Do(func() {
		_ = (<-c.conn).CloseNow()
		out = []byte("output")
	})
	if out != nil {
		return out, nil
	}
	return c.fakeTransport.Receive(ctx, n, timeout)
}

func TestBridgeProxyStopsWhenTheBrowserIsGone(t *testing.T) {
	accepted := make(chan *websocket.Conn, 1)
	tr := &closingTransport{fakeTransport: newFakeTransport(), conn: accepted}
	url := proxyBridgeServer(t, tr, accepted)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	c, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer c.CloseNow() //nolint:errcheck // test cleanup
	if _, _, err := c.Read(ctx); err == nil {
		t.Fatal("output reached a browser whose socket was closed")
	}
}

// An SSH proxy session dials with host-key verification off (the proxy has no
// host-key surface) and reports an unreachable upstream to the browser.
func TestProxyHandlerSSHUpstreamUnreachable(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	_ = ln.Close()
	srv := httptest.NewServer(proxyHandler(proxyOptions{Host: "127.0.0.1", BBSPort: port, Path: "/ws", Transport: "ssh"}))
	defer srv.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	c, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(srv.URL, "http")+"/ws", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer c.CloseNow() //nolint:errcheck // test cleanup
	if _, _, err := c.Read(ctx); websocket.CloseStatus(err) != websocket.StatusInternalError {
		t.Fatalf("read = %v, want an internal-error close", err)
	}
}

// The proxy command parses its arguments and serves until its context ends.
func TestProxyCommandServesUntilCancelled(t *testing.T) {
	cmd := newProxyCmd()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	cmd.SetContext(ctx)
	cmd.SetArgs([]string{"example.invalid", "23", "--port", "0", "--bind", "127.0.0.1"})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("proxy = %v", err)
	}
}

// writeFailConn refuses writes; its reads block until it is closed.
type writeFailConn struct {
	net.Conn
	closed chan struct{}
	once   sync.Once
}

func (c *writeFailConn) Read([]byte) (int, error) {
	<-c.closed
	return 0, io.EOF
}

func (c *writeFailConn) Write([]byte) (int, error) { return 0, errResidual }

func (c *writeFailConn) Close() error {
	c.once.Do(func() { close(c.closed) })
	return nil
}

// Tunnel bytes the local connection will not take end the relay.
func TestHandleTunnelConnEndsWhenLocalWriteFails(t *testing.T) {
	f := newFakeTunnelServer(t, func(ctx context.Context, c *websocket.Conn) {
		_ = c.Write(ctx, websocket.MessageBinary,
			tunnelclient.EncodeFrame(tunnelclient.ChannelTCP, []byte("reply"), tunnelclient.FlagData))
		for {
			if _, _, err := c.Read(ctx); err != nil {
				return
			}
		}
	})
	client := tunnelclient.NewClient("ws"+strings.TrimPrefix(f.srv.URL, "http")+"/tunnel", "")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := client.Connect(ctx); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = client.Close() }()
	conn := &writeFailConn{closed: make(chan struct{})}
	handleTunnelConn(ctx, conn, client)
	if ctx.Err() != nil {
		t.Fatal("relay only ended on the test timeout")
	}
}

// --attach on a real terminal puts it in raw mode for the share and hands it
// back restored on Close.
func TestOpenShareSourceAttachOnATerminal(t *testing.T) {
	ptmx, tty, err := pty.Open()
	if err != nil {
		t.Fatalf("open pty: %v", err)
	}
	defer func() { _ = ptmx.Close(); _ = tty.Close() }()
	before, err := xterm.GetState(int(tty.Fd()))
	if err != nil {
		t.Fatal(err)
	}
	orig := os.Stdin
	os.Stdin = tty
	t.Cleanup(func() { os.Stdin = orig })

	src, err := openShareSource(shareOptions{Attach: true})
	if err != nil {
		t.Fatalf("attach on a terminal: %v", err)
	}
	proxy, ok := src.(*tunnelclient.TtyProxy)
	if !ok || !proxy.Active() {
		t.Fatalf("source = %T active=%v, want an active TtyProxy", src, ok && proxy.Active())
	}
	if err := src.Close(); err != nil {
		t.Fatal(err)
	}
	after, err := xterm.GetState(int(tty.Fd()))
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(before, after) {
		t.Fatal("Close did not restore the terminal state")
	}
}
