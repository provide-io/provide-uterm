//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"errors"
	"log/slog"
	"math"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/frames"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// msgSink is a slog handler that publishes every record's message.
type msgSink struct{ msgs chan string }

func newMsgSink() *msgSink                                { return &msgSink{msgs: make(chan string, 1024)} }
func (*msgSink) Enabled(context.Context, slog.Level) bool { return true }
func (s *msgSink) WithAttrs([]slog.Attr) slog.Handler     { return s }
func (s *msgSink) WithGroup(string) slog.Handler          { return s }
func (s *msgSink) Handle(_ context.Context, r slog.Record) error {
	select {
	case s.msgs <- r.Message:
	default:
	}
	return nil
}

// wait blocks until msg is logged (or fails the test after a generous bound).
func (s *msgSink) wait(t *testing.T, msg string) {
	t.Helper()
	deadline := time.After(10 * time.Second)
	for {
		select {
		case got := <-s.msgs:
			if got == msg {
				return
			}
		case <-deadline:
			t.Fatalf("log %q never emitted", msg)
		}
	}
}

// seen reports whether msg was logged so far (non-blocking drain).
func (s *msgSink) seen(msg string) bool {
	for {
		select {
		case got := <-s.msgs:
			if got == msg {
				return true
			}
		default:
			return false
		}
	}
}

func TestControlMsgAndPromptIDHelpers(t *testing.T) {
	m := controlMsg("pause", "op", 30, 1.5, "h-1")
	if m["hijack_id"] != "h-1" || m["action"] != "pause" {
		t.Fatalf("controlMsg = %v", m)
	}
	if _, ok := controlMsg("step", "op", 0, 1, "")["hijack_id"]; ok {
		t.Fatal("empty hijack id must be omitted")
	}
	if got := extractPromptID(map[string]any{"prompt_detected": map[string]any{"prompt_id": ""}}); got != nil {
		t.Fatalf("empty prompt id = %v, want nil", got)
	}
}

// arrayFrame claims a frame type but serializes to a JSON array.
type arrayFrame struct {
	Type string `json:"type"`
}

func (arrayFrame) FrameType() string            { return "array" }
func (arrayFrame) MarshalJSON() ([]byte, error) { return []byte(`[1]`), nil }

var _ frames.Frame = arrayFrame{}

func TestEncodeFrameControlErrors(t *testing.T) {
	if _, err := encodeFrameControl("not a frame"); err == nil {
		t.Fatal("non-frame value encoded")
	}
	if _, err := encodeFrameControl(arrayFrame{Type: "array"}); err == nil {
		t.Fatal("non-object frame encoded")
	}
}

// countingServer counts requests and records their paths.
func countingServer(t *testing.T, handler http.HandlerFunc) (*httptest.Server, *atomic.Int64) {
	t.Helper()
	var hits atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		if handler != nil {
			handler(w, r)
		}
	}))
	t.Cleanup(srv.Close)
	return srv, &hits
}

func TestWebhookRecordingStoreSkipsUnencodableWrite(t *testing.T) {
	var actions []string
	var mu sync.Mutex
	srv, hits := countingServer(t, func(_ http.ResponseWriter, r *http.Request) {
		buf := make([]byte, r.ContentLength)
		_, _ = r.Body.Read(buf)
		mu.Lock()
		actions = append(actions, string(buf))
		mu.Unlock()
	})
	store := NewWebhookRecordingStore(srv.URL, "", 2*time.Second, NewEgressGuard(nil, nil))
	if err := store.StartSession("s1", map[string]any{"bad": make(chan int)}); err != nil {
		t.Fatal(err)
	}
	if err := store.EndSession("s1"); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	if hits.Load() != 1 || len(actions) != 1 || !strings.Contains(actions[0], `"action":"end"`) {
		t.Fatalf("hits=%d actions=%q; only the encodable end must be posted", hits.Load(), actions)
	}
}

func TestWebhookRecordingStoreReadFailuresAreEmpty(t *testing.T) {
	bad := NewWebhookRecordingStore("http://127.0.0.1:1/\x7f", "", time.Second, NewEgressGuard(nil, nil))
	if meta, err := bad.RecordingMeta("s1"); err != nil || meta != (recording.Meta{SessionID: "s1"}) {
		t.Fatalf("meta = %+v, %v", meta, err)
	}
	if err := bad.EndSession("s1"); err != nil { // the request cannot even be built
		t.Fatal(err)
	}
	trunc := truncatedServer(t)
	store := NewWebhookRecordingStore(trunc.URL, "", 2*time.Second, NewEgressGuard(nil, nil))
	if entries, err := store.GetEntries("s1", recording.Query{}); err != nil || len(entries) != 0 {
		t.Fatalf("entries = %v, %v; want empty", entries, err)
	}
}

// truncatedServer promises more body than it sends.
func truncatedServer(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Length", "100")
		_, _ = w.Write([]byte(`{"entries":`))
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestDiscoveryAnnounceLocalFailures(t *testing.T) {
	srv, hits := countingServer(t, nil)
	p := NewWebhookDiscoveryProvider(srv.URL, "", 2, nil)
	if err := p.Announce(context.Background(), NodeStatus{NodeID: "n", Timestamp: math.NaN()}); err == nil {
		t.Fatal("unencodable status announced")
	}
	bad := NewWebhookDiscoveryProvider("http://127.0.0.1:1/\x7f", "", 2, nil)
	if err := bad.Announce(context.Background(), NodeStatus{NodeID: "n"}); err == nil {
		t.Fatal("malformed URL announced")
	}
	if hits.Load() != 0 {
		t.Fatalf("discovery endpoint hit %d times", hits.Load())
	}
}

func TestEgressGuardDefaultsResolveAndTime(t *testing.T) {
	if err := NewEgressGuard(nil, nil).AssertConnectorTargetAllowed(context.Background(), "localhost", false); err != nil {
		t.Fatalf("default resolver refused localhost: %v", err)
	}
	ts := newTestServer(t, nil)
	// A DNS-name webhook target goes through the TTL cache, which reads the
	// server-clock-backed now().
	if err := ts.srv.egress.AssertWebhookTargetAllowed(context.Background(), "http://localhost:9/hook"); err != nil {
		t.Fatalf("server guard refused localhost webhook: %v", err)
	}
}

func TestFanoutAuthorizerReadMemberWithoutPrincipal(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("w1", "admin1", "public")
	a := fanoutAuthorizer{registry: ts.reg, authz: ts.srv.deps.Authz}
	if a.CanReadMember(context.Background(), nil, "w1") {
		t.Fatal("nil principal can read a member")
	}
}

// acceptFunc is a listener whose Accept is supplied by the test.
type acceptFunc struct {
	net.Listener
	accept func() (net.Conn, error)
}

func (l acceptFunc) Accept() (net.Conn, error) { return l.accept() }

func serveWith(t *testing.T, ts *testServer, accept func() (net.Conn, error)) error {
	t.Helper()
	real, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = real.Close() })
	ctx, cancel := context.WithCancel(context.Background())
	defer func() { cancel(); ts.srv.sweepWG.Wait() }()
	return ts.srv.Serve(ctx, acceptFunc{Listener: real, accept: accept})
}

func TestServeReturnsListenerFailureAndWarnsInTestMode(t *testing.T) {
	t.Setenv(testModeEnvVar, "1")
	sink := newMsgSink()
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.Logger = slog.New(sink) })
	broken := errors.New("accept broke")
	if err := serveWith(t, ts, func() (net.Conn, error) { return nil, broken }); !errors.Is(err, broken) {
		t.Fatalf("Serve = %v; want the accept error", err)
	}
	if !sink.seen(testModeWarning) {
		t.Fatal("test-mode warning not logged")
	}
}

// closeSignalListener reports when Shutdown closes it.
type closeSignalListener struct {
	net.Listener
	closed chan struct{}
	once   sync.Once
}

func (l *closeSignalListener) Close() error {
	l.once.Do(func() { close(l.closed) })
	return l.Listener.Close()
}

func TestServeReturnsNilAfterShutdown(t *testing.T) {
	ts := newTestServer(t, nil)
	real, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ln := &closeSignalListener{Listener: real, closed: make(chan struct{})}
	shutdownDone := make(chan error, 1)
	var once sync.Once
	wrapped := acceptFunc{Listener: ln, accept: func() (net.Conn, error) {
		// Shutdown waits for Serve to return, so it cannot run on this
		// goroutine; wait instead until it has closed the listener.
		once.Do(func() { go func() { shutdownDone <- ts.srv.httpSrv.Shutdown(context.Background()) }() })
		<-ln.closed
		return nil, net.ErrClosed
	}}
	ctx, cancel := context.WithCancel(context.Background())
	defer func() { cancel(); ts.srv.sweepWG.Wait() }()
	if err := ts.srv.Serve(ctx, wrapped); err != nil {
		t.Fatalf("Serve after Shutdown = %v; want nil", err)
	}
	if err := <-shutdownDone; err != nil {
		t.Fatalf("Shutdown = %v", err)
	}
}

func TestPamCaptureSocketConfinedResolveFailures(t *testing.T) {
	dir := "/var/run/uterm-capture"
	socket := filepath.Join(dir, "cap.sock")
	for _, failOn := range []string{socket, dir} {
		t.Run(filepath.Base(failOn), func(t *testing.T) {
			log := &recordingLogger{}
			pi := NewPamIntegration(serverconfig.PamConfig{CaptureSocketDir: strptr(dir)}, newFakeRegistry(), nil, log)
			orig := filepathAbs
			filepathAbs = func(p string) (string, error) {
				if p == failOn {
					return "", errors.New("getwd failed")
				}
				return orig(p)
			}
			t.Cleanup(func() { filepathAbs = orig })
			if pi.captureSocketConfined(socket) {
				t.Fatal("unresolvable path treated as confined")
			}
			if !log.has("pam_capture_socket_confined_resolve_failed") {
				t.Fatal("resolve failure not logged")
			}
		})
	}
}

func TestPamRelayCallsFailBeforeSending(t *testing.T) {
	srv, hits := countingServer(t, nil)
	log := &recordingLogger{}
	pi := NewPamIntegration(serverconfig.PamConfig{Mode: "notify", RelayURL: strptr(srv.URL), RelayToken: strptr("t")},
		newFakeRegistry(), nil, log)
	pi.forwardToRelay(context.Background(), map[string]any{"bad": make(chan int)})
	if !log.has("pam_relay_forward_failed") || hits.Load() != 0 {
		t.Fatalf("unencodable event: logged=%v hits=%d", log.has("pam_relay_forward_failed"), hits.Load())
	}

	log2 := &recordingLogger{}
	bad := NewPamIntegration(serverconfig.PamConfig{Mode: "notify", RelayURL: strptr("http://127.0.0.1:1/\x7f"), RelayToken: strptr("t")},
		newFakeRegistry(), nil, log2)
	bad.forwardToRelay(context.Background(), map[string]any{"event": "open"})
	if !log2.has("pam_relay_forward_failed") {
		t.Fatal("malformed relay URL not logged on forward")
	}
	if tok, ep, err := bad.createRelayTunnel(context.Background(), "s1", "S1"); err == nil || tok != "" || ep != "" {
		t.Fatalf("createRelayTunnel = %q %q %v; want request error", tok, ep, err)
	}
	if !log2.has("create_relay_tunnel_failed") {
		t.Fatal("malformed relay URL not logged on tunnel create")
	}
}

func TestRecordingPathAllowedResolutionFailures(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "rec.jsonl")
	if err := os.WriteFile(file, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if !recordingPathAllowed(file, dir) {
		t.Fatal("file inside dir refused")
	}
	if recordingPathAllowed(file, filepath.Join(dir, "missing")) {
		t.Fatal("missing directory accepted")
	}
	origAbs, origRel := filepathAbs, filepathRel
	t.Cleanup(func() { filepathAbs, filepathRel = origAbs, origRel })
	for call := 1; call <= 2; call++ {
		n := 0
		filepathAbs = func(p string) (string, error) {
			n++
			if n == call {
				return "", errors.New("getwd failed")
			}
			return origAbs(p)
		}
		if recordingPathAllowed(file, dir) {
			t.Fatalf("Abs failure on call %d accepted", call)
		}
	}
	filepathAbs = origAbs
	filepathRel = func(string, string) (string, error) { return "", errors.New("different volumes") }
	if recordingPathAllowed(file, dir) {
		t.Fatal("Rel failure accepted")
	}
}

func TestRecordingRoutesRejectInvalidSessionID(t *testing.T) {
	ts := newTestServer(t, nil)
	rec := ts.do("GET", "/api/sessions/bad%20id/recording", "", adminHeaders())
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("invalid id = %d %s; want 422", rec.Code, rec.Body.String())
	}
}

func TestWebhookDeliveryRefusedWhileTunnelShareIsLive(t *testing.T) {
	srv, hits := countingServer(t, func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	var mu sync.Mutex
	metrics := map[string]int{}
	m := NewWebhookRegistry(WebhookOptions{
		Guard:                     NewEgressGuard(nil, nil),
		AllowLoopbackDestinations: true,
		HasLiveTunnelShare:        func(id string) bool { return id == "shared" },
		OnMetric: func(name string, v int) {
			mu.Lock()
			metrics[name] += v
			mu.Unlock()
		},
		Logger: quietLogger(),
	})
	t.Cleanup(m.Shutdown)
	m.deliver(&webhookEntry{id: "wh1", sessionID: "shared", url: srv.URL}, map[string]any{"type": "output"})
	mu.Lock()
	blocked := metrics["webhook_delivery_blocked_tunnel_total"]
	mu.Unlock()
	if blocked != 1 || hits.Load() != 0 {
		t.Fatalf("shared session: blocked=%d hits=%d; want 1, 0", blocked, hits.Load())
	}
	m.deliver(&webhookEntry{id: "wh2", sessionID: "private", url: srv.URL}, map[string]any{"type": "output"})
	if hits.Load() != 1 {
		t.Fatalf("unshared session: hits=%d; want 1", hits.Load())
	}
}

func TestRegistryHeartbeatDefaultIntervalAndFailureLog(t *testing.T) {
	orig := defaultRegistryHeartbeatInterval
	defaultRegistryHeartbeatInterval = time.Millisecond
	t.Cleanup(func() { defaultRegistryHeartbeatInterval = orig })
	sink := newMsgSink()
	dead := "http://127.0.0.1:1/announce" // refused: the announce fails
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, deps *Deps) {
		cfg.Governance.DiscoveryProvider = "webhook"
		cfg.Governance.RegistryWebhookURL = &dead
		cfg.Governance.RegistryWebhookIntervalS = 0
		deps.Logger = slog.New(sink)
	})
	ctx, cancel := context.WithCancel(context.Background())
	ts.srv.startNodeRegistryHeartbeat(ctx)
	sink.wait(t, "node_registry_heartbeat_failed")
	cancel()
	ts.srv.sweepWG.Wait()
}
