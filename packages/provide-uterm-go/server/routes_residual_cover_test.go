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
	"sync"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/fanout"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

func adminPrincipal() *serverauth.Principal {
	return &serverauth.Principal{SubjectID: "admin1", Roles: serverauth.NewSet("admin"), Scopes: serverauth.NewSet("*")}
}

// principalRequest builds a request already past authentication.
func principalRequest(ctx context.Context, method, target, body string, p *serverauth.Principal) *http.Request {
	req := httptest.NewRequestWithContext(ctx, method, target, strings.NewReader(body))
	return req.WithContext(withPrincipal(req.Context(), p))
}

func TestCreateAPIKeyRejectsInvalidTenant(t *testing.T) {
	ts := newTestServer(t, nil)
	rec := ts.do("POST", "/api/keys", `{"name":"k","scopes":["viewer"]}`, map[string]string{"X-Subject": "a", "X-Role": "admin", "X-Tenant": "bad tenant!"})
	if rec.Code != http.StatusUnprocessableEntity || !strings.Contains(rec.Body.String(), "tenant") {
		t.Fatalf("invalid tenant = %d %s; want 422", rec.Code, rec.Body.String())
	}
	if keys := ts.apiKeys.ListKeys(); len(keys) != 0 {
		t.Fatalf("key minted despite invalid tenant: %v", keys)
	}
}

// failingOutputGate makes every content broadcast fail.
type failingOutputGate struct{}

func (failingOutputGate) GetRedactionRules(context.Context, hub.PolicyContext) ([]hub.RedactionRule, error) {
	return nil, errors.New("gate down")
}

func TestRejectApprovalResolvesEvenWhenTheNoticeFails(t *testing.T) {
	sink := newMsgSink()
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Logger = slog.New(sink)
		deps.Hub = hub.NewTermHub(hub.TermHubConfig{
			Clock: deps.Clock, OutputPolicyGate: failingOutputGate{}, OnMetric: deps.Metrics.Inc, Logger: deps.Logger,
		})
	})
	ts.setupWorker(t, "w1")
	if _, err := ts.hub.RegisterBrowser(context.Background(), "w1", &browserConn{}, "admin", false); err != nil {
		t.Fatal(err)
	}
	now := ts.srv.clock.Wall()
	if !ts.hub.Approvals.Add(&hub.ApprovalRequest{ID: "req-1", WorkerID: "w1", Command: "rm -rf /",
		Status: hub.ApprovalPending, CreatedAt: now, ExpiresAt: now + 60}) {
		t.Fatal("seed approval")
	}
	rec := ts.do("POST", "/api/approvals/req-1/reject", "", adminHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("reject = %d %s; want 200", rec.Code, rec.Body.String())
	}
	if !sink.seen("resolve_approval_failed") {
		t.Fatal("broadcast failure not logged")
	}
	if req := ts.hub.Approvals.Get("req-1"); req == nil || req.Status != hub.ApprovalRejected {
		t.Fatalf("approval = %+v; want rejected", req)
	}
}

func fanoutSendDirect(ts *testServer, groupID string, p *serverauth.Principal) *httptest.ResponseRecorder {
	req := principalRequest(context.Background(), "POST", "/api/fanout/groups/"+groupID+"/send", `{"data":"ls"}`, p)
	req.SetPathValue("group_id", groupID)
	rec := httptest.NewRecorder()
	ts.srv.handleFanoutSend(rec, req)
	return rec
}

func TestFanoutSendHandlerRechecksGlobalAdmin(t *testing.T) {
	// The route wraps the handler in fanoutAdmin; the controller re-checks on
	// its own, which is what a grantee reaching the handler directly meets.
	ts := newTestServer(t, nil)
	ts.reg.add("w1", "admin1", "public")
	if _, err := ts.srv.fanout.CreateGroup(&fanout.Group{GroupID: "g1", WorkerIDs: []string{"w1"}, Grants: []string{"op1"}}, "admin1"); err != nil {
		t.Fatal(err)
	}
	rec := fanoutSendDirect(ts, "g1", &serverauth.Principal{SubjectID: "op1", Roles: serverauth.NewSet("operator")})
	if rec.Code != http.StatusForbidden || !strings.Contains(rec.Body.String(), "admin role required") {
		t.Fatalf("grantee send = %d %s; want 403", rec.Code, rec.Body.String())
	}
}

func TestFanoutSendHandlerWithoutAuthorizerIsUnavailable(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.srv.fanout = fanout.NewController(ts.hub, fanout.Config{})
	if _, err := ts.srv.fanout.CreateGroup(&fanout.Group{GroupID: "g1", WorkerIDs: []string{"w1"}}, "admin1"); err != nil {
		t.Fatal(err)
	}
	rec := fanoutSendDirect(ts, "g1", adminPrincipal())
	if rec.Code != http.StatusServiceUnavailable || !strings.Contains(rec.Body.String(), fanout.ErrAuthorizerUnavailable.Error()) {
		t.Fatalf("send = %d %s; want 503", rec.Code, rec.Body.String())
	}
}

func TestGraphicalTargetConfigMustBeAnObject(t *testing.T) {
	ts := newTestServer(t, nil)
	rec := ts.do("POST", "/api/graphical-targets", `{"protocol":"rfb","endpoint":"vm.local:5900","config":"x"}`, tenantHeaders("operator", "acme"))
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("config string = %d %s; want 422", rec.Code, rec.Body.String())
	}
}

func TestToGraphicalDefinitionRejectsLitevirtWithoutPort(t *testing.T) {
	def, err := toGraphicalDefinition(serverconfig.GraphicalTargetConfig{TargetID: "t", Protocol: "litevirt", TargetAddress: "vm.local"})
	if err == nil || def != nil {
		t.Fatalf("toGraphicalDefinition = %+v, %v; want endpoint error", def, err)
	}
}

// partialRegistry lists incomplete rows alongside the real ones.
type partialRegistry struct{ *fakeRegistry }

func (r partialRegistry) ListWithDefinitions(ctx context.Context) []SessionListItem {
	items := r.fakeRegistry.ListWithDefinitions(ctx)
	return append(items,
		SessionListItem{Status: nil, Definition: &serverconfig.SessionDefinition{SessionID: "no-status"}},
		SessionListItem{Status: &SessionStatus{SessionID: "no-def"}, Definition: nil})
}

// keepAuthz refuses to let anyone mutate the session named "keep".
type keepAuthz struct {
	serverauth.LocalAuthorizationProvider
}

func (k keepAuthz) CanMutateSession(p *serverauth.Principal, s *serverconfig.SessionDefinition, action string) bool {
	return s.SessionID != "keep" && k.LocalAuthorizationProvider.CanMutateSession(p, s, action)
}

func partialServer(t *testing.T) *testServer {
	t.Helper()
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Registry = partialRegistry{deps.Registry.(*fakeRegistry)}
		deps.Authz = serverauth.NewAuthorizationServiceWith(keepAuthz{})
	})
	ts.reg.add("keep", "admin1", "public")
	ts.reg.add("gone", "admin1", "public")
	return ts
}

func TestListSessionsSkipsIncompleteRows(t *testing.T) {
	ts := partialServer(t)
	rec := ts.do("GET", "/api/sessions", "", adminHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("list = %d %s", rec.Code, rec.Body.String())
	}
	body := rec.Body.String()
	if strings.Contains(body, "no-status") || strings.Contains(body, "no-def") || !strings.Contains(body, `"gone"`) {
		t.Fatalf("list body = %s", body)
	}
}

func TestBulkDeleteSkipsIncompleteAndProtectedRows(t *testing.T) {
	ts := partialServer(t)
	rec := ts.do("DELETE", "/api/sessions", `{"filter":{}}`, adminHeaders())
	if rec.Code != http.StatusOK || decode(t, rec.Body.Bytes())["deleted"] != float64(1) {
		t.Fatalf("bulk delete = %d %s; want deleted=1", rec.Code, rec.Body.String())
	}
	ts.reg.mu.Lock()
	defer ts.reg.mu.Unlock()
	if len(ts.reg.deleted) != 1 || ts.reg.deleted[0] != "gone" {
		t.Fatalf("deleted = %v; want [gone]", ts.reg.deleted)
	}
}

func TestSessionRoutesRejectUnknownAndInvalidIDs(t *testing.T) {
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Webhooks = NewWebhookRegistry(WebhookOptions{Logger: quietLogger()})
	})
	ts.reg.add("s1", "admin1", "public")
	cases := []struct {
		method, path string
		want         int
	}{
		{"DELETE", "/api/sessions/ghost", http.StatusNotFound},
		{"POST", "/api/sessions/bad%20id/connect", http.StatusUnprocessableEntity},
		{"GET", "/api/sessions/bad%20id/snapshot", http.StatusUnprocessableEntity},
		{"DELETE", "/api/sessions/s1/webhooks/bad%20id", http.StatusUnprocessableEntity},
		{"DELETE", "/api/sessions/ghost/webhooks/wh1", http.StatusNotFound},
	}
	for _, c := range cases {
		if rec := ts.do(c.method, c.path, "", adminHeaders()); rec.Code != c.want {
			t.Fatalf("%s %s = %d %s; want %d", c.method, c.path, rec.Code, rec.Body.String(), c.want)
		}
	}
}

// sseRecorder is a non-flushing ResponseWriter that reports the header write
// (the subscription is live by then) and every body write; failWrites makes
// every body write fail as a vanished client's would.
type sseRecorder struct {
	header     http.Header
	failWrites bool
	started    chan struct{}
	startOnce  sync.Once
	writes     chan string
}

func newSSERecorder(failWrites bool) *sseRecorder {
	return &sseRecorder{header: http.Header{}, failWrites: failWrites, started: make(chan struct{}), writes: make(chan string, 256)}
}

func (r *sseRecorder) Header() http.Header { return r.header }
func (r *sseRecorder) WriteHeader(int)     { r.startOnce.Do(func() { close(r.started) }) }
func (r *sseRecorder) Write(p []byte) (int, error) {
	r.WriteHeader(http.StatusOK)
	if r.failWrites {
		return 0, errors.New("client gone")
	}
	select {
	case r.writes <- string(p):
	default:
	}
	return len(p), nil
}

// waitWrite blocks until a body write containing sub arrives, returning every
// write seen before it.
func (r *sseRecorder) waitWrite(t *testing.T, sub string) []string {
	t.Helper()
	var seen []string
	deadline := time.After(10 * time.Second)
	for {
		select {
		case w := <-r.writes:
			seen = append(seen, w)
			if strings.Contains(w, sub) {
				return seen
			}
		case <-deadline:
			t.Fatalf("no write containing %q (saw %q)", sub, seen)
		}
	}
}

func setHeartbeat(t *testing.T, d time.Duration) {
	t.Helper()
	orig := sseHeartbeatInterval
	sseHeartbeatInterval = d
	t.Cleanup(func() { sseHeartbeatInterval = orig })
}

// runStream serves the SSE stream for s1 on a goroutine; done closes on return.
func runStream(ts *testServer, ctx context.Context, w http.ResponseWriter) <-chan struct{} {
	req := principalRequest(ctx, "GET", "/api/sessions/s1/events/stream", "", adminPrincipal())
	req.SetPathValue("session_id", "s1")
	done := make(chan struct{})
	go func() { ts.srv.handleEventStream(w, req); close(done) }()
	return done
}

func sseServer(t *testing.T) *testServer {
	t.Helper()
	ts := newTestServer(t, nil)
	ts.reg.add("s1", "admin1", "public")
	return ts
}

func TestEventStreamRejectsInvalidPattern(t *testing.T) {
	ts := sseServer(t)
	rec := ts.do("GET", "/api/sessions/s1/events/stream?pattern=(", "", adminHeaders())
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("bad pattern = %d %s; want 500", rec.Code, rec.Body.String())
	}
}

func TestEventStreamSendsHeartbeats(t *testing.T) {
	setHeartbeat(t, time.Millisecond)
	ts := sseServer(t)
	ctx, cancel := context.WithCancel(context.Background())
	w := newSSERecorder(false)
	done := runStream(ts, ctx, w)
	w.waitWrite(t, `"heartbeat"`)
	cancel()
	<-done
}

func TestEventStreamEndsWhenHeartbeatWriteFails(t *testing.T) {
	setHeartbeat(t, time.Millisecond)
	ts := sseServer(t)
	<-runStream(ts, context.Background(), newSSERecorder(true)) // returns on its own
}

func TestEventStreamSkipsUnencodableEvents(t *testing.T) {
	setHeartbeat(t, time.Hour)
	ts := sseServer(t)
	ctx, cancel := context.WithCancel(context.Background())
	w := newSSERecorder(false)
	done := runStream(ts, ctx, w)
	<-w.started
	bus := ts.hub.EventBus()
	bus.Enqueue("s1", map[string]any{"type": "output", "marker": "bad", "ch": make(chan int)})
	bus.Enqueue("s1", map[string]any{"type": "output", "marker": "good"})
	for _, seen := range w.waitWrite(t, `"good"`) {
		if strings.Contains(seen, "bad") {
			t.Fatalf("unencodable event written: %q", seen)
		}
	}
	cancel()
	<-done
}

func TestEventStreamEndsWhenEventWriteFails(t *testing.T) {
	setHeartbeat(t, time.Hour)
	ts := sseServer(t)
	w := newSSERecorder(true)
	done := runStream(ts, context.Background(), w)
	<-w.started
	ts.hub.EventBus().Enqueue("s1", map[string]any{"type": "output"})
	<-done // the failed write ends the stream without any cancellation
}
