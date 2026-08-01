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
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// holdGate is a PolicyGate that holds every input for approval.
type holdGate struct{}

func (holdGate) InterceptInput(_ context.Context, _ string, _ hub.PolicyContext) (hub.PolicyDecision, error) {
	return hub.PolicyDecision{Action: "hold", TimeoutS: 60}, nil
}

// recSession is a bridge.Session that records the input forwarded to the worker.
type recSession struct {
	mu       sync.Mutex
	watchers []bridge.WatchFunc
	sends    []string
}

func (s *recSession) AddWatch(fn bridge.WatchFunc) {
	s.mu.Lock()
	s.watchers = append(s.watchers, fn)
	s.mu.Unlock()
}
func (s *recSession) Send(_ context.Context, data string) error {
	s.mu.Lock()
	s.sends = append(s.sends, data)
	s.mu.Unlock()
	return nil
}
func (s *recSession) SetSize(context.Context, int, int) error { return nil }
func (s *recSession) Snapshot() map[string]any {
	return map[string]any{"screen": "", "cols": 80, "rows": 25}
}
func (s *recSession) received(sub string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, d := range s.sends {
		if strings.Contains(d, sub) {
			return true
		}
	}
	return false
}
func (s *recSession) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.sends)
}

type recWorker struct{ session *recSession }

func (w *recWorker) Session() bridge.Session                 { return w.session }
func (w *recWorker) SetHijacked(context.Context, bool) error { return nil }
func (w *recWorker) RequestStep(context.Context) error       { return nil }

// startWorkerBridge dials a real worker bridge into the server.
func startWorkerBridge(ctx context.Context, url, workerID string, session *recSession) *bridge.TermBridge {
	br := bridge.New(bridge.Config{
		Worker:     &recWorker{session: session},
		WorkerID:   workerID,
		ManagerURL: url,
		InputMode:  "hijack",
		Encoding:   "latin-1",
	})
	br.Start(ctx)
	return br
}

// newApprovalServer builds a test server whose hub holds every input for
// approval, returning the server and the hub it actually uses.
func newApprovalServer(t *testing.T) (*testServer, *hub.TermHub) {
	t.Helper()
	var appHub *hub.TermHub
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		appHub = hub.NewTermHub(hub.TermHubConfig{
			Clock:      deps.Clock,
			PolicyGate: holdGate{},
			OnMetric:   deps.Metrics.Inc,
			Logger:     deps.Logger,
		})
		deps.Hub = appHub
	})
	return ts, appHub
}

func dialApprovalOwner(t *testing.T, ctx context.Context, url string) *browserClient {
	t.Helper()
	bc := dialBrowser(t, ctx, url, "approval-submitter", "admin")
	bc.waitFrame(t, "hello", 5*time.Second)
	bc.send(t, ctx, map[string]any{"type": "hijack_request"})
	bc.waitFrameWhere(t, "hijack_state", 5*time.Second, func(frame map[string]any) bool {
		return frame["owner"] == "me"
	})
	return bc
}

// TestApprovalRequiredInputApproveReachesWorker drives: a browser input frame is
// held for approval → approval_pending broadcast → REST approve → the held
// command reaches the worker → approval_resolved(approved).
func TestApprovalRequiredInputApproveReachesWorker(t *testing.T) {
	ts, appHub := newApprovalServer(t)
	ts.srv.MarkReady()
	ts.reg.add("appr", "admin1", "public")

	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	session := &recSession{}
	br := startWorkerBridge(ctx, httpSrv.URL, "appr", session)
	defer br.Stop()
	waitUntil(t, 5*time.Second, func() bool { return appHub.Registry.Contains("appr") })

	// The submitting browser is a (non-admin) viewer, so the admin approver is a
	// different principal (no self-approval conflict).
	bc := dialApprovalOwner(t, ctx, wsBase+"/ws/browser/appr/term")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()

	bc.send(t, ctx, map[string]any{"type": "input", "data": "whoami\n"})
	pending := bc.waitFrame(t, "approval_pending", 5*time.Second)
	reqID, _ := pending["request_id"].(string)
	if reqID == "" || pending["command"] != "whoami\n" {
		t.Fatalf("approval_pending frame: %v", pending)
	}
	if session.count() != 0 {
		t.Fatal("held command must not reach the worker before approval")
	}

	rec := ts.do("POST", "/api/approvals/"+reqID+"/approve", "", adminHeaders())
	if rec.Code != 200 {
		t.Fatalf("approve status = %d, body=%s", rec.Code, rec.Body.String())
	}

	waitUntil(t, 5*time.Second, func() bool { return session.received("whoami") })
	resolved := bc.waitFrameWhere(t, "approval_resolved", 5*time.Second, func(f map[string]any) bool {
		return f["request_id"] == reqID
	})
	if resolved["outcome"] != "approved" {
		t.Fatalf("approval_resolved outcome = %v", resolved["outcome"])
	}
}

// TestApprovalRequiredInputRejectSendsBanner drives: held input → REST reject →
// red rejection banner (raw terminal data) + approval_resolved(rejected), and
// the command never reaches the worker.
func TestApprovalRequiredInputRejectSendsBanner(t *testing.T) {
	ts, appHub := newApprovalServer(t)
	ts.srv.MarkReady()
	ts.reg.add("rej", "admin1", "public")

	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	session := &recSession{}
	br := startWorkerBridge(ctx, httpSrv.URL, "rej", session)
	defer br.Stop()
	waitUntil(t, 5*time.Second, func() bool { return appHub.Registry.Contains("rej") })

	bc := dialApprovalOwner(t, ctx, wsBase+"/ws/browser/rej/term")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()

	bc.send(t, ctx, map[string]any{"type": "input", "data": "shutdown\n"})
	pending := bc.waitFrame(t, "approval_pending", 5*time.Second)
	reqID, _ := pending["request_id"].(string)

	rec := ts.do("POST", "/api/approvals/"+reqID+"/reject", "", adminHeaders())
	if rec.Code != 200 {
		t.Fatalf("reject status = %d, body=%s", rec.Code, rec.Body.String())
	}

	// The red banner arrives on the raw-terminal-data channel.
	waitUntil(t, 5*time.Second, func() bool {
		select {
		case d := <-bc.data:
			return strings.Contains(d, "[REJECTED] Command 'shutdown' blocked by Admin.")
		case <-time.After(200 * time.Millisecond):
			return false
		}
	})
	resolved := bc.waitFrameWhere(t, "approval_resolved", 5*time.Second, func(f map[string]any) bool {
		return f["request_id"] == reqID
	})
	if resolved["outcome"] != "rejected" {
		t.Fatalf("approval_resolved outcome = %v", resolved["outcome"])
	}
	if session.count() != 0 {
		t.Fatal("rejected command must never reach the worker")
	}
}

// TestApprovalRejectUnknownIsBadRequest covers the not-pending 400 path (a
// second resolve of an already-resolved request).
func TestApprovalDoubleResolveIsBadRequest(t *testing.T) {
	ts, appHub := newApprovalServer(t)
	ts.srv.MarkReady()
	ts.reg.add("dbl", "admin1", "public")

	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	session := &recSession{}
	br := startWorkerBridge(ctx, httpSrv.URL, "dbl", session)
	defer br.Stop()
	waitUntil(t, 5*time.Second, func() bool { return appHub.Registry.Contains("dbl") })

	bc := dialApprovalOwner(t, ctx, wsBase+"/ws/browser/dbl/term")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
	bc.send(t, ctx, map[string]any{"type": "input", "data": "ls\n"})
	reqID, _ := bc.waitFrame(t, "approval_pending", 5*time.Second)["request_id"].(string)

	if rec := ts.do("POST", "/api/approvals/"+reqID+"/approve", "", adminHeaders()); rec.Code != 200 {
		t.Fatalf("first approve = %d", rec.Code)
	}
	if rec := ts.do("POST", "/api/approvals/"+reqID+"/approve", "", adminHeaders()); rec.Code != 400 {
		t.Fatalf("second approve should be 400 (not pending), got %d", rec.Code)
	}
}

// denyGate is a PolicyGate that denies every input.
type denyGate struct{}

func (denyGate) InterceptInput(_ context.Context, _ string, _ hub.PolicyContext) (hub.PolicyDecision, error) {
	return hub.PolicyDecision{Action: "deny", TimeoutS: 60}, nil
}

// TestApprovalListEndpoint covers GET /api/approvals (admin lists pending;
// viewer is forbidden).
func TestApprovalListEndpoint(t *testing.T) {
	ts, appHub := newApprovalServer(t)
	ts.srv.MarkReady()
	ts.reg.add("lst", "admin1", "public")

	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	session := &recSession{}
	br := startWorkerBridge(ctx, httpSrv.URL, "lst", session)
	defer br.Stop()
	waitUntil(t, 5*time.Second, func() bool { return appHub.Registry.Contains("lst") })

	bc := dialApprovalOwner(t, ctx, wsBase+"/ws/browser/lst/term")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
	bc.send(t, ctx, map[string]any{"type": "input", "data": "id\n"})
	reqID, _ := bc.waitFrame(t, "approval_pending", 5*time.Second)["request_id"].(string)

	// Viewer is forbidden.
	if rec := ts.do("GET", "/api/approvals", "", viewerHeaders()); rec.Code != 403 {
		t.Fatalf("viewer list should be 403, got %d", rec.Code)
	}
	// Admin sees the pending request.
	rec := ts.do("GET", "/api/approvals", "", adminHeaders())
	if rec.Code != 200 {
		t.Fatalf("admin list status = %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), reqID) || !strings.Contains(rec.Body.String(), "\"status\":\"pending\"") {
		t.Fatalf("list body = %s", rec.Body.String())
	}
}

// TestBrowserInputDenyGate covers the policy-deny branch of browserInputGated.
func TestBrowserInputDenyGate(t *testing.T) {
	var appHub *hub.TermHub
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		appHub = hub.NewTermHub(hub.TermHubConfig{
			Clock: deps.Clock, PolicyGate: denyGate{}, OnMetric: deps.Metrics.Inc, Logger: deps.Logger,
		})
		deps.Hub = appHub
	})
	ts.srv.MarkReady()
	ts.reg.add("deny", "admin1", "public")

	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	session := &recSession{}
	br := startWorkerBridge(ctx, httpSrv.URL, "deny", session)
	defer br.Stop()
	waitUntil(t, 5*time.Second, func() bool { return appHub.Registry.Contains("deny") })

	bc := dialApprovalOwner(t, ctx, wsBase+"/ws/browser/deny/term")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
	bc.send(t, ctx, map[string]any{"type": "input", "data": "rm\n"})
	bc.waitFrameWhere(t, "error", 5*time.Second, func(f map[string]any) bool {
		msg, _ := f["message"].(string)
		return strings.Contains(msg, "blocked by policy")
	})
	if session.count() != 0 {
		t.Fatal("denied input must not reach the worker")
	}
}

// TestBrowserInputParkedBufferingAndTooLong covers the parked-browser branches:
// a second input while parked is buffered (replayed on approve), and an
// over-limit hold buffer yields an error.
func TestBrowserInputParkedBufferingAndTooLong(t *testing.T) {
	ts, appHub := newApprovalServer(t)
	ts.srv.MarkReady()
	ts.reg.add("park", "admin1", "public")

	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	session := &recSession{}
	br := startWorkerBridge(ctx, httpSrv.URL, "park", session)
	defer br.Stop()
	waitUntil(t, 5*time.Second, func() bool { return appHub.Registry.Contains("park") })

	bc := dialApprovalOwner(t, ctx, wsBase+"/ws/browser/park/term")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()

	// First input is held for approval → browser is parked.
	bc.send(t, ctx, map[string]any{"type": "input", "data": "base\n"})
	reqID, _ := bc.waitFrame(t, "approval_pending", 5*time.Second)["request_id"].(string)

	// A small second input while parked is buffered (no new approval_pending).
	bc.send(t, ctx, map[string]any{"type": "input", "data": "more\n"})
	// An over-limit buffered input yields an "Input too long" error.
	bc.send(t, ctx, map[string]any{"type": "input", "data": strings.Repeat("z", 41000)})
	bc.waitFrameWhere(t, "error", 5*time.Second, func(f map[string]any) bool {
		msg, _ := f["message"].(string)
		return strings.Contains(msg, "Input too long")
	})

	// Approve → the original command and the buffered "more" both reach the worker.
	if rec := ts.do("POST", "/api/approvals/"+reqID+"/approve", "", adminHeaders()); rec.Code != 200 {
		t.Fatalf("approve = %d", rec.Code)
	}
	waitUntil(t, 5*time.Second, func() bool { return session.received("base") && session.received("more") })
}

func TestApprovalAfterOwnershipLossReturnsConflictWithoutReplay(t *testing.T) {
	ts, appHub := newApprovalServer(t)
	ts.srv.MarkReady()
	ts.reg.add("approval-lost-owner", "admin1", "public")

	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	session := &recSession{}
	br := startWorkerBridge(ctx, httpSrv.URL, "approval-lost-owner", session)
	defer br.Stop()
	waitUntil(t, 5*time.Second, func() bool { return appHub.Registry.Contains("approval-lost-owner") })

	bc := dialApprovalOwner(t, ctx, wsBase+"/ws/browser/approval-lost-owner/term")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
	bc.send(t, ctx, map[string]any{"type": "input", "data": "base\n"})
	reqID, _ := bc.waitFrame(t, "approval_pending", 5*time.Second)["request_id"].(string)
	bc.send(t, ctx, map[string]any{"type": "input", "data": "buffered\n"})
	bc.send(t, ctx, map[string]any{"type": "hijack_release"})
	bc.send(t, ctx, map[string]any{"type": "ping"})
	bc.waitFrame(t, "pong", 5*time.Second)

	rec := ts.do("POST", "/api/approvals/"+reqID+"/approve", "", adminHeaders())
	if rec.Code != http.StatusConflict {
		t.Fatalf("approve after ownership loss = %d, body=%s", rec.Code, rec.Body.String())
	}
	if session.received("base") || session.received("buffered") || session.count() != 0 {
		t.Fatal("lost-owner approval delivered command or replay")
	}
	if status := string(appHub.Approvals.Get(reqID).Status); status != "refused" {
		t.Fatalf("approval terminal status = %q", status)
	}
}

// TestBrowserInputOversizedBeforeGate covers the pre-gate length check.
func TestBrowserInputOversizedBeforeGate(t *testing.T) {
	ts, appHub := newApprovalServer(t)
	ts.srv.MarkReady()
	ts.reg.add("big", "admin1", "public")

	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	session := &recSession{}
	br := startWorkerBridge(ctx, httpSrv.URL, "big", session)
	defer br.Stop()
	waitUntil(t, 5*time.Second, func() bool { return appHub.Registry.Contains("big") })

	bc := dialApprovalOwner(t, ctx, wsBase+"/ws/browser/big/term")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
	bc.send(t, ctx, map[string]any{"type": "input", "data": strings.Repeat("x", 11000)})
	bc.waitFrameWhere(t, "error", 5*time.Second, func(f map[string]any) bool {
		msg, _ := f["message"].(string)
		return strings.Contains(msg, "Input too long")
	})
}

// errGate is a PolicyGate whose interception fails.
type errGate struct{}

func (errGate) InterceptInput(_ context.Context, _ string, _ hub.PolicyContext) (hub.PolicyDecision, error) {
	return hub.PolicyDecision{}, context.DeadlineExceeded
}

// TestApprovalRestEdgeCases covers 404 (unknown id) for approve/reject and the
// reject reason query param, plus the intercept-error branch.
func TestApprovalRestEdgeCases(t *testing.T) {
	ts, appHub := newApprovalServer(t)
	ts.srv.MarkReady()
	ts.reg.add("edge", "admin1", "public")

	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	session := &recSession{}
	br := startWorkerBridge(ctx, httpSrv.URL, "edge", session)
	defer br.Stop()
	waitUntil(t, 5*time.Second, func() bool { return appHub.Registry.Contains("edge") })

	// Unknown request ids → 404 on both verbs.
	if rec := ts.do("POST", "/api/approvals/nope/approve", "", adminHeaders()); rec.Code != 404 {
		t.Fatalf("approve unknown = %d", rec.Code)
	}
	if rec := ts.do("POST", "/api/approvals/nope/reject", "", adminHeaders()); rec.Code != 404 {
		t.Fatalf("reject unknown = %d", rec.Code)
	}

	// Reject with a reason query param exercises the reason branch.
	bc := dialApprovalOwner(t, ctx, wsBase+"/ws/browser/edge/term")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
	bc.send(t, ctx, map[string]any{"type": "input", "data": "danger\n"})
	reqID, _ := bc.waitFrame(t, "approval_pending", 5*time.Second)["request_id"].(string)
	if rec := ts.do("POST", "/api/approvals/"+reqID+"/reject?reason=too-risky", "", adminHeaders()); rec.Code != 200 {
		t.Fatalf("reject with reason = %d", rec.Code)
	}
	waitUntil(t, 5*time.Second, func() bool {
		select {
		case d := <-bc.data:
			return strings.Contains(d, "Reason: too-risky")
		case <-time.After(200 * time.Millisecond):
			return false
		}
	})
}

// TestBrowserInputGateError covers the intercept-error branch (input is dropped,
// the socket stays usable).
func TestBrowserInputGateError(t *testing.T) {
	var appHub *hub.TermHub
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		appHub = hub.NewTermHub(hub.TermHubConfig{
			Clock: deps.Clock, PolicyGate: errGate{}, OnMetric: deps.Metrics.Inc, Logger: deps.Logger,
		})
		deps.Hub = appHub
	})
	ts.srv.MarkReady()
	ts.reg.add("ge", "admin1", "public")

	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	session := &recSession{}
	br := startWorkerBridge(ctx, httpSrv.URL, "ge", session)
	defer br.Stop()
	waitUntil(t, 5*time.Second, func() bool { return appHub.Registry.Contains("ge") })

	bc := dialApprovalOwner(t, ctx, wsBase+"/ws/browser/ge/term")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
	bc.send(t, ctx, map[string]any{"type": "input", "data": "x\n"}) // gate errors → dropped
	// The socket is still usable: a ping is answered.
	bc.send(t, ctx, map[string]any{"type": "ping"})
	bc.waitFrame(t, "pong", 5*time.Second)
	if session.count() != 0 {
		t.Fatal("input dropped on gate error must not reach the worker")
	}
}
