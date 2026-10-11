//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/fanout"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// wsPair returns the server side of a live WebSocket as a browserConn, plus a
// reading client for the frames written to it.
func wsPair(t *testing.T, p *serverauth.Principal) (*browserConn, *browserClient) {
	t.Helper()
	accepted := make(chan *websocket.Conn, 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		accepted <- c
	}))
	t.Cleanup(srv.Close)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	client := dialBrowserWithHeaders(t, ctx, "ws"+strings.TrimPrefix(srv.URL, "http"), nil)
	t.Cleanup(func() { _ = client.conn.CloseNow() })
	server := <-accepted
	t.Cleanup(func() { _ = server.CloseNow() })
	return &browserConn{wsBase: wsBase{conn: server}, principal: p}, client
}

// gatedInputServer wires gate into the hub and registers worker w1 with an
// admin browser that owns the dashboard hijack.
func gatedInputServer(t *testing.T, gate hub.PolicyGate) (*testServer, *lifecycleRecordingWorker, *browserConn, *msgSink) {
	t.Helper()
	sink := newMsgSink()
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Logger = slog.New(sink)
		deps.Hub = hub.NewTermHub(hub.TermHubConfig{Clock: deps.Clock, PolicyGate: gate, OnMetric: deps.Metrics.Inc, Logger: deps.Logger})
	})
	worker := &lifecycleRecordingWorker{}
	browser := &browserConn{}
	ctx := context.Background()
	if _, err := ts.hub.RegisterWorker(ctx, "w1", worker); err != nil {
		t.Fatal(err)
	}
	if _, err := ts.hub.RegisterBrowser(ctx, "w1", browser, "admin", true); err != nil {
		t.Fatal(err)
	}
	if ok, reason := ts.hub.TryAcquireWsHijack(ctx, "w1", browser); !ok {
		t.Fatalf("acquire: %s", reason)
	}
	return ts, worker, browser, sink
}

func workerGot(w *lifecycleRecordingWorker, sub string) bool {
	for _, p := range w.snapshot() {
		if strings.Contains(p, sub) {
			return true
		}
	}
	return false
}

type allowGate struct{}

func (allowGate) InterceptInput(context.Context, string, hub.PolicyContext) (hub.PolicyDecision, error) {
	return hub.PolicyDecision{Action: "allow", TimeoutS: 60}, nil
}

// fixedIDHoldGate holds every input under one caller-chosen request id.
type fixedIDHoldGate struct{ id string }

func (g fixedIDHoldGate) InterceptInput(context.Context, string, hub.PolicyContext) (hub.PolicyDecision, error) {
	return hub.PolicyDecision{Action: "hold", RequestID: &g.id, TimeoutS: 60}, nil
}

func TestBrowserInputGatedIgnoresEmptyInput(t *testing.T) {
	ts, worker, browser, _ := gatedInputServer(t, allowGate{})
	before := len(worker.snapshot())
	ts.srv.browserInputGated(context.Background(), "w1", browser, map[string]any{"type": "input"})
	if got := len(worker.snapshot()); got != before {
		t.Fatalf("empty input produced %d worker sends", got-before)
	}
}

func TestBrowserInputGatedForwardsAllowedInput(t *testing.T) {
	ts, worker, browser, _ := gatedInputServer(t, allowGate{})
	ts.srv.browserInputGated(context.Background(), "w1", browser, map[string]any{"type": "input", "data": "ls\n"})
	if !workerGot(worker, "ls\n") {
		t.Fatalf("allowed input not delivered: %q", worker.snapshot())
	}
}

func TestBrowserInputGatedParkFailureDropsInput(t *testing.T) {
	ts, worker, browser, sink := gatedInputServer(t, fixedIDHoldGate{id: "dup"})
	now := ts.srv.clock.Wall()
	if !ts.hub.Approvals.Add(&hub.ApprovalRequest{ID: "dup", WorkerID: "w1", Status: hub.ApprovalPending, CreatedAt: now, ExpiresAt: now + 60}) {
		t.Fatal("seed approval")
	}
	ts.srv.browserInputGated(context.Background(), "w1", browser, map[string]any{"type": "input", "data": "rm\n"})
	if !sink.seen("park_for_approval_failed") {
		t.Fatal("park failure not logged")
	}
	if ts.hub.IsBrowserParked(browser) || workerGot(worker, "rm\n") {
		t.Fatal("failed park must neither park the browser nor deliver the input")
	}
}

func stubHandshakeSend(t *testing.T, fn func(n int) error) *atomic.Int64 {
	t.Helper()
	var calls atomic.Int64
	orig := handshakeSend
	handshakeSend = func(context.Context, *browserConn, string) error { return fn(int(calls.Add(1))) }
	t.Cleanup(func() { handshakeSend = orig })
	return &calls
}

func TestBrowserHandshakeStopsWhenHijackStateSendFails(t *testing.T) {
	ts := newTestServer(t, nil)
	calls := stubHandshakeSend(t, func(n int) error {
		if n == 2 {
			return errors.New("socket gone")
		}
		return nil
	})
	ok := ts.srv.browserHandshake(context.Background(), nil, "w1", &browserConn{}, "admin", true, map[string]any{"input_mode": "hijack"})
	if ok || calls.Load() != 2 {
		t.Fatalf("handshake = %v after %d sends; want false after 2", ok, calls.Load())
	}
}

// serveBrowserOnce runs one browser socket through the real handler and waits
// for the handler to return.
func serveBrowserOnce(t *testing.T, ts *testServer, headers http.Header) {
	t.Helper()
	done := make(chan struct{})
	var once atomicOnce
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ts.srv.Handler().ServeHTTP(w, r)
		once.do(func() { close(done) })
	}))
	t.Cleanup(srv.Close)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	conn, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(srv.URL, "http")+"/ws/browser/w1/term", &websocket.DialOptions{HTTPHeader: headers})
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = conn.CloseNow() }()
	select {
	case <-done:
	case <-ctx.Done():
		t.Fatal("handler did not return after the failed handshake")
	}
}

type atomicOnce struct{ v atomic.Bool }

func (o *atomicOnce) do(fn func()) {
	if o.v.CompareAndSwap(false, true) {
		fn()
	}
}

func TestBrowserWSCleansUpWhenHandshakeFails(t *testing.T) {
	for _, testMode := range []bool{false, true} {
		t.Run(map[bool]string{false: "auth", true: "test-mode"}[testMode], func(t *testing.T) {
			if testMode {
				t.Setenv("UTERM_TEST_MODE", "1")
			}
			ts := newTestServer(t, nil)
			ts.reg.add("w1", "admin1", "public")
			calls := stubHandshakeSend(t, func(int) error { return errors.New("socket gone") })
			serveBrowserOnce(t, ts, http.Header{"X-Subject": {"admin1"}, "X-Role": {"admin"}})
			if calls.Load() != 1 {
				t.Fatalf("handshake sends = %d, want 1", calls.Load())
			}
			if n := ts.hub.BrowserCount(context.Background(), "w1"); n != 0 {
				t.Fatalf("browser left registered after failed handshake: %d", n)
			}
		})
	}
}

func TestDeckFailuresAreLoggedAndUnencodableSyncIsNotSent(t *testing.T) {
	sink := newMsgSink()
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.Logger = slog.New(sink) })
	ts.setupWorker(t, "w1") // broadcasts need a registered worker to reach encoding
	ctx := context.Background()
	a, aClient := wsPair(t, &serverauth.Principal{SubjectID: "ua"})
	ts.srv.deckOnConnect(ctx, a, "w1", "admin")
	aClient.waitFrame(t, "presence_sync", 5*time.Second)

	// A presence field that cannot be encoded poisons the broadcast.
	ts.srv.deckHandle("w1", a, map[string]any{"type": "presence_update", "scroll_range": make(chan int)})
	if !sink.seen("deckmux_handle_failed") {
		t.Fatal("presence broadcast failure not logged")
	}
	// The second browser's sync carries the poisoned user: its broadcast fails
	// (logged) and the sync itself cannot be encoded, so nothing is written to
	// the browser — which has no socket, so a write would panic.
	ts.srv.deckOnConnect(ctx, &browserConn{principal: &serverauth.Principal{SubjectID: "ub"}}, "w1", "viewer")
	if !sink.seen("deckmux_connect_failed") {
		t.Fatal("presence connect failure not logged")
	}
}

func fanoutWSServer(t *testing.T, opt func(*serverconfig.UtermServerConfig)) *testServer {
	t.Helper()
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
		if opt != nil {
			opt(cfg)
		}
	})
	ts.reg.add("w1", "admin1", "public")
	return ts
}

func expectErrorFrame(t *testing.T, client *browserClient, want string) {
	t.Helper()
	f := client.waitFrame(t, "error", 5*time.Second)
	if msg, _ := f["message"].(string); !strings.Contains(msg, want) {
		t.Fatalf("error frame = %v; want %q", f, want)
	}
}

func TestBrowserFanoutSendAnonymousIsRefused(t *testing.T) {
	ts := fanoutWSServer(t, nil)
	bc, client := wsPair(t, nil)
	ts.srv.browserFanoutSend(context.Background(), bc, map[string]any{"group_id": "g1", "data": "ls"})
	expectErrorFrame(t, client, "admin role required")
}

func TestBrowserFanoutSendGovernanceUnsupported(t *testing.T) {
	hook := "https://policy.example/hook"
	ts := fanoutWSServer(t, func(cfg *serverconfig.UtermServerConfig) { cfg.Governance.PolicyWebhookURL = &hook })
	if _, err := ts.srv.fanout.CreateGroup(&fanout.Group{GroupID: "g1", WorkerIDs: []string{"w1"}}, "admin1"); err != nil {
		t.Fatal(err)
	}
	bc, client := wsPair(t, adminPrincipal())
	ts.srv.browserFanoutSend(context.Background(), bc, map[string]any{"group_id": "g1", "data": "ls"})
	expectErrorFrame(t, client, unsupportedFanoutGovernance)
}

func TestBrowserFanoutSendControllerFailure(t *testing.T) {
	ts := fanoutWSServer(t, nil)
	ts.srv.fanout = fanout.NewController(ts.hub, fanout.Config{})
	if _, err := ts.srv.fanout.CreateGroup(&fanout.Group{GroupID: "g1", WorkerIDs: []string{"w1"}}, "admin1"); err != nil {
		t.Fatal(err)
	}
	bc, client := wsPair(t, adminPrincipal())
	ts.srv.browserFanoutSend(context.Background(), bc, map[string]any{"group_id": "g1", "data": "ls"})
	expectErrorFrame(t, client, fanout.ErrAuthorizerUnavailable.Error())
}

func TestBrowserResumeGivesUpWhileTokenIsStillAttached(t *testing.T) {
	ts := resumeTestServer(t)
	ts.reg.add("w1", "admin1", "public")
	holder := &browserConn{}
	state, err := ts.hub.RegisterBrowser(context.Background(), "w1", holder, "admin", false)
	if err != nil {
		t.Fatal(err)
	}
	token, _ := state["resume_token"].(string)
	if token == "" {
		t.Fatal("no resume token minted")
	}
	// The holder is still connected, so the token never detaches; a cancelled
	// wait gives up without consuming it (and without writing to the socketless
	// resumer).
	ts.srv.browserResume(cancelledCtx(), nil, "w1", &browserConn{}, "admin", map[string]any{"token": token})
	if stored, err := ts.hub.ResumeStore().Get(context.Background(), token); err != nil || stored == nil {
		t.Fatalf("token consumed by an aborted resume: %v %v", stored, err)
	}
}

func TestWriteFrameSkipsNonFrames(t *testing.T) {
	ts := newTestServer(t, nil)
	// A socketless browser would panic on any write; the encode failure must
	// stop before one.
	ts.srv.writeFrame(context.Background(), &browserConn{}, "not a frame")
}

func TestTunnelWSRejectsPlainHTTP(t *testing.T) {
	ts := newTestServer(t, nil)
	rec := ts.do("GET", "/tunnel/w1", "", nil)
	if rec.Code == http.StatusSwitchingProtocols || rec.Code < 400 {
		t.Fatalf("plain GET = %d; want an upgrade refusal", rec.Code)
	}
	if ts.hub.Registry.Contains("w1") {
		t.Fatal("tunnel worker registered without a WebSocket")
	}
}

func TestTunnelSendHTTPControlEncodeError(t *testing.T) {
	wc := &tunnelWorkerConn{}
	if err := wc.SendHTTPControl(context.Background(), map[string]any{"type": "x", "bad": make(chan int)}); err == nil {
		t.Fatal("unencodable control sent")
	}
}
