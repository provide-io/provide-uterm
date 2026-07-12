//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnelclient"
)

// originServer returns an httptest origin and its port.
func originServer(t *testing.T, h http.HandlerFunc) int {
	t.Helper()
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	_, portStr, _ := net.SplitHostPort(strings.TrimPrefix(srv.URL, "http://"))
	port, _ := strconv.Atoi(portStr)
	return port
}

// wsHTTPEvents parses a ChannelHTTP tunnel frame into its JSON object.
func decodeHTTPEvent(raw []byte) (map[string]any, bool) {
	frame, err := tunnelclient.DecodeFrame(raw)
	if err != nil || frame.Channel != tunnelclient.ChannelHTTP {
		return nil, false
	}
	var obj map[string]any
	if json.Unmarshal(frame.Payload, &obj) != nil {
		return nil, false
	}
	return obj, true
}

func TestRunInspectProxiesAndInspects(t *testing.T) {
	targetPort := originServer(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, "hello-origin")
	})

	events := make(chan map[string]any, 32)
	f := newFakeTunnelServer(t, func(ctx context.Context, c *websocket.Conn) {
		for {
			_, raw, err := c.Read(ctx)
			if err != nil {
				return
			}
			if ev, ok := decodeHTTPEvent(raw); ok {
				events <- ev
			}
		}
	})

	listenPort := freePort(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var out, errw bytes.Buffer
	runErr := make(chan error, 1)
	go func() {
		runErr <- runInspect(ctx, inspectOptions{
			Server: f.srv.URL, Port: targetPort, ListenPort: listenPort,
			InterceptTimeout: 30, InterceptTimeoutAction: "forward",
		}, &out, &errw)
	}()

	// Drive a request through the proxy.
	resp := getWithRetry(t, listenPort, "/ping?x=1")
	if resp.status != 200 || resp.body != "hello-origin" {
		t.Fatalf("proxy response = %d %q", resp.status, resp.body)
	}

	// Expect an http_req then an http_res inspection event.
	sawReq, sawRes := false, false
	deadline := time.Now().Add(3 * time.Second)
	for (!sawReq || !sawRes) && time.Now().Before(deadline) {
		select {
		case ev := <-events:
			switch ev["type"] {
			case "http_req":
				sawReq = true
				if ev["method"] != "GET" || ev["url"] != "/ping?x=1" {
					t.Fatalf("http_req event = %v", ev)
				}
			case "http_res":
				sawRes = true
				if ev["status"] != float64(200) {
					t.Fatalf("http_res status = %v", ev["status"])
				}
			}
		case <-time.After(200 * time.Millisecond):
		}
	}
	if !sawReq || !sawRes {
		t.Fatalf("missing inspection events: req=%v res=%v", sawReq, sawRes)
	}
	// stderr log lines were emitted for the exchange.
	if !strings.Contains(errw.String(), "GET /ping") {
		t.Fatalf("missing stderr log line: %q", errw.String())
	}

	cancel()
	select {
	case err := <-runErr:
		if err != nil {
			t.Fatalf("runInspect: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("runInspect did not return after cancel")
	}
}

func TestInspectInterceptDrop(t *testing.T) {
	var hit atomic.Bool
	targetPort := originServer(t, func(w http.ResponseWriter, r *http.Request) {
		hit.Store(true)
		_, _ = io.WriteString(w, "should-not-reach")
	})

	// The tunnel server drops every intercepted request by replying with an
	// http_action{drop} carrying the http_req's id (loop so retries work).
	f := newFakeTunnelServer(t, func(ctx context.Context, c *websocket.Conn) {
		for {
			_, raw, err := c.Read(ctx)
			if err != nil {
				return
			}
			ev, ok := decodeHTTPEvent(raw)
			if !ok || ev["type"] != "http_req" {
				continue
			}
			action, _ := json.Marshal(map[string]any{"type": "http_action", "id": ev["id"], "action": "drop"})
			_ = c.Write(ctx, websocket.MessageBinary, tunnelclient.EncodeFrame(tunnelclient.ChannelHTTP, action, tunnelclient.FlagData))
		}
	})

	client := tunnelclient.NewClient("ws"+strings.TrimPrefix(f.srv.URL, "http")+"/tunnel", "")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := client.Connect(ctx); err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer func() { _ = client.Close() }()

	// Short timeout + retry: same race class as TestInspectInterceptModify.
	gate := tunnelclient.NewInterceptGate(2, "forward")
	gate.SetEnabled(true)
	sess := &inspectSession{client: client, gate: gate, targetPort: targetPort, errw: io.Discard}

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	proxyPort := ln.Addr().(*net.TCPAddr).Port
	go func() { _ = sess.serve(ctx, ln) }()

	deadline := time.Now().Add(8 * time.Second)
	var last httpResult
	for time.Now().Before(deadline) {
		last = tryGet(proxyPort, "/secret")
		if last.status == http.StatusBadGateway && strings.Contains(last.body, "dropped by interceptor") {
			if hit.Load() {
				t.Fatal("origin must not be reached for a dropped request")
			}
			return
		}
		time.Sleep(25 * time.Millisecond)
	}
	t.Fatalf("drop never applied: status=%d body=%q targetHit=%v", last.status, last.body, hit.Load())
}

func TestInspectBadGateway(t *testing.T) {
	// Target port with nothing listening → 502 Bad Gateway.
	dead := freePort(t)
	sess := &inspectSession{
		client:     mustConnectDiscardTunnel(t),
		gate:       tunnelclient.NewInterceptGate(30, "forward"),
		targetPort: dead,
		errw:       io.Discard,
	}
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	proxyPort := ln.Addr().(*net.TCPAddr).Port
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sess.serve(ctx, ln) }()

	resp := getWithRetry(t, proxyPort, "/x")
	if resp.status != http.StatusBadGateway || !strings.Contains(resp.body, "Bad Gateway") {
		t.Fatalf("expected 502 Bad Gateway, got %d %q", resp.status, resp.body)
	}
}

// mustConnectDiscardTunnel connects a client to a ws server that just drains.
func mustConnectDiscardTunnel(t *testing.T) *tunnelclient.Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer c.CloseNow() //nolint:errcheck // test cleanup
		for {
			if _, _, err := c.Read(r.Context()); err != nil {
				return
			}
		}
	}))
	t.Cleanup(srv.Close)
	client := tunnelclient.NewClient("ws"+strings.TrimPrefix(srv.URL, "http"), "")
	if err := client.Connect(context.Background()); err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(func() { _ = client.Close() })
	return client
}

// httpResult is a minimal captured proxy response.
type httpResult struct {
	status  int
	body    string
	headers http.Header
}

func (r httpResult) headerGet(k string) string {
	if r.headers == nil {
		return ""
	}
	return r.headers.Get(k)
}

// tryGet performs one GET against the local proxy. On dial errors it returns
// status 0 so callers can retry without failing the test.
func tryGet(port int, path string) httpResult {
	url := "http://127.0.0.1:" + strconv.Itoa(port) + path
	resp, err := http.Get(url) //nolint:noctx,gosec // test-local URL
	if err != nil {
		return httpResult{}
	}
	body, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	return httpResult{status: resp.StatusCode, body: string(body), headers: resp.Header.Clone()}
}

func getWithRetry(t *testing.T, port int, path string) httpResult {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	var last httpResult
	for time.Now().Before(deadline) {
		last = tryGet(port, path)
		if last.status != 0 {
			return last
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("GET http://127.0.0.1:%d%s failed: no successful response", port, path)
	return httpResult{}
}

func TestInspectHelpers(t *testing.T) {
	if round1(42.049) != 42.0 {
		t.Fatalf("round1 = %v", round1(42.049))
	}
	if round1(42.06) != 42.1 {
		t.Fatalf("round1 = %v", round1(42.06))
	}
	if boolOf(nil, true) != true || boolOf(false, true) != false || boolOf(true, false) != true {
		t.Fatal("boolOf coercion wrong")
	}
	if statusText(404) != "Not Found" {
		t.Fatalf("statusText = %q", statusText(404))
	}
	h := http.Header{"Content-Type": {"a", "b"}, "X-Empty": nil}
	flat := flattenHeaders(h)
	if flat["content-type"] != "b" {
		t.Fatalf("flatten last-wins = %q", flat["content-type"])
	}
	if _, ok := flat["x-empty"]; ok {
		t.Fatal("empty-valued header should be skipped")
	}
	filtered := filterHeaders(map[string]string{"host": "x", "keep": "y"}, hopHeadersOut)
	if _, ok := filtered["host"]; ok {
		t.Fatal("host should be filtered")
	}
	if filtered["keep"] != "y" {
		t.Fatal("keep header dropped")
	}
}

func TestDecodeActionMessage(t *testing.T) {
	// text path: only known types accepted
	if _, ok := decodeActionMessage(true, []byte(`{"type":"nonsense"}`)); ok {
		t.Fatal("unknown text type should be rejected")
	}
	if m, ok := decodeActionMessage(true, []byte(`{"type":"http_action","id":"r1"}`)); !ok || m["id"] != "r1" {
		t.Fatalf("http_action text should parse: %v %v", m, ok)
	}
	if _, ok := decodeActionMessage(true, []byte("not json")); ok {
		t.Fatal("invalid text json should be rejected")
	}
	// binary path: too short / wrong channel / valid
	if _, ok := decodeActionMessage(false, []byte{0x03}); ok {
		t.Fatal("short binary should be rejected")
	}
	wrong := tunnelclient.EncodeFrame(tunnelclient.ChannelData, []byte(`{"type":"http_action"}`), tunnelclient.FlagData)
	if _, ok := decodeActionMessage(false, wrong); ok {
		t.Fatal("non-HTTP channel should be rejected")
	}
	good := tunnelclient.EncodeFrame(tunnelclient.ChannelHTTP, []byte(`{"type":"http_intercept_toggle","enabled":true}`), tunnelclient.FlagData)
	if m, ok := decodeActionMessage(false, good); !ok || m["type"] != "http_intercept_toggle" {
		t.Fatalf("binary HTTP frame should parse: %v %v", m, ok)
	}
}

func TestDispatchActionToggles(t *testing.T) {
	sess := &inspectSession{
		client: mustConnectDiscardTunnel(t),
		gate:   tunnelclient.NewInterceptGate(30, "forward"),
		errw:   io.Discard,
	}
	ctx := context.Background()

	sess.dispatchAction(ctx, map[string]any{"type": "http_intercept_toggle", "enabled": true})
	if !sess.gate.Enabled() {
		t.Fatal("intercept toggle on failed")
	}
	sess.dispatchAction(ctx, map[string]any{"type": "http_intercept_toggle", "enabled": false})
	if sess.gate.Enabled() {
		t.Fatal("intercept toggle off failed")
	}
	// inspect toggle off also forces intercept off
	sess.gate.SetEnabled(true)
	sess.dispatchAction(ctx, map[string]any{"type": "http_inspect_toggle", "enabled": false})
	if sess.gate.InspectEnabled() || sess.gate.Enabled() {
		t.Fatal("inspect toggle off should disable both")
	}
	// http_action for an unknown id is a no-op (must not panic)
	sess.dispatchAction(ctx, map[string]any{"type": "http_action", "id": "ghost", "action": "forward"})
}

func TestInspectArgParseError(t *testing.T) {
	var out, errw bytes.Buffer
	if code := Execute([]string{"inspect", "notaport", "-s", "http://x"}, &out, &errw); code == 0 {
		t.Fatal("non-integer PORT should fail")
	}
}
