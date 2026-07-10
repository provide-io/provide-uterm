//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"bufio"
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// --- SSE ------------------------------------------------------------------

func TestSSEStream(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("s1", "admin1", "public")
	httpSrv := httptest.NewServer(ts.srv.Handler())

	ctx, cancel := context.WithCancel(context.Background())
	req, _ := http.NewRequestWithContext(ctx, "GET", httpSrv.URL+"/api/sessions/s1/events/stream", http.NoBody)
	req.Header.Set("X-Subject", "admin1")
	req.Header.Set("X-Role", "admin")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("stream do: %v", err)
	}
	// Cleanup order matters: cancel the request + close the body FIRST so the
	// SSE handler's context fires, THEN close the server (which waits for the
	// handler to return).
	defer func() {
		cancel()
		_ = resp.Body.Close()
		httpSrv.Close()
	}()
	if ct := resp.Header.Get("Content-Type"); ct != "text/event-stream" {
		t.Fatalf("sse content-type: %q", ct)
	}
	// The subscription is registered once headers are written; enqueue an event.
	ts.hub.EventBus().Enqueue("s1", map[string]any{"type": "snapshot", "screen": "hi"})

	line := make(chan string, 1)
	go func() {
		reader := bufio.NewReader(resp.Body)
		for {
			l, e := reader.ReadString('\n')
			if e != nil {
				return
			}
			if strings.HasPrefix(l, "data: ") {
				line <- l
				return
			}
		}
	}()
	select {
	case l := <-line:
		if !strings.Contains(l, "snapshot") {
			t.Fatalf("sse data line: %q", l)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("no SSE data received")
	}
	cancel()
}

func TestSSEForbiddenAndNoBus(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("priv", "admin1", "private")
	if rec := ts.do("GET", "/api/sessions/priv/events/stream", "", viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("viewer stream priv: %d", rec.Code)
	}

	// No-bus server: the stream returns an immediate empty 200.
	cfg := serverconfig.DefaultServerConfig()
	h := hub.NewTermHub(hub.TermHubConfig{})
	reg := newFakeRegistry()
	reg.add("s1", "admin1", "public")
	srv, err := New(Deps{Hub: h, Auth: fakeAuth{}, Authz: serverauth.NewAuthorizationService(), Config: cfg, Registry: reg})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/api/sessions/s1/events/stream", http.NoBody)
	req.Header.Set("X-Subject", "admin1")
	req.Header.Set("X-Role", "admin")
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK || rec.Body.Len() != 0 {
		t.Fatalf("no-bus stream: %d %q", rec.Code, rec.Body.String())
	}
}

// --- Webhooks -------------------------------------------------------------

type fakeWebhooks struct {
	urlErr error
	patErr error
	regErr error
	items  []map[string]any
	get    map[string]any
	getOK  bool
}

func (f *fakeWebhooks) ValidateURL(string) error     { return f.urlErr }
func (f *fakeWebhooks) ValidatePattern(string) error { return f.patErr }
func (f *fakeWebhooks) Register(sessionID, url string, et []string, pattern, _ string) (map[string]any, error) {
	if f.regErr != nil {
		return nil, f.regErr
	}
	return map[string]any{"webhook_id": "wh1", "session_id": sessionID, "url": url, "event_types": et, "pattern": pattern}, nil
}
func (f *fakeWebhooks) ListWebhooks(string) []map[string]any { return f.items }
func (f *fakeWebhooks) GetWebhook(string) (map[string]any, bool) {
	return f.get, f.getOK
}
func (f *fakeWebhooks) Unregister(string) bool { return true }

func TestWebhooks(t *testing.T) {
	wh := &fakeWebhooks{items: []map[string]any{{"webhook_id": "wh1"}}, get: map[string]any{"session_id": "s1"}, getOK: true}
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.Webhooks = wh })
	ts.reg.add("s1", "admin1", "public")

	// Missing url → 422.
	if rec := ts.do("POST", "/api/sessions/s1/webhooks", `{}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("missing url: %d", rec.Code)
	}
	// event_types not list → 422.
	if rec := ts.do("POST", "/api/sessions/s1/webhooks", `{"url":"http://x","event_types":"x"}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("bad event_types: %d", rec.Code)
	}
	// Register ok.
	if rec := ts.do("POST", "/api/sessions/s1/webhooks", `{"url":"http://x","event_types":["a"],"pattern":"p"}`, adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("register: %d %s", rec.Code, rec.Body.String())
	}
	// invalid url → 422.
	wh.urlErr = &SessionValidationError{Msg: "bad url"}
	if rec := ts.do("POST", "/api/sessions/s1/webhooks", `{"url":"http://x"}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("bad url: %d", rec.Code)
	}
	wh.urlErr = nil
	// List.
	if rec := ts.do("GET", "/api/sessions/s1/webhooks", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("list: %d", rec.Code)
	}
	// Unregister ok.
	if rec := ts.do("DELETE", "/api/sessions/s1/webhooks/wh1", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("unregister: %d", rec.Code)
	}
	// Unregister unknown → 404.
	wh.getOK = false
	if rec := ts.do("DELETE", "/api/sessions/s1/webhooks/ghost", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("unregister unknown: %d", rec.Code)
	}

	// No manager → 503.
	ns := newTestServer(t, nil)
	ns.reg.add("s1", "admin1", "public")
	if rec := ns.do("GET", "/api/sessions/s1/webhooks", "", adminHeaders()); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("no manager: %d", rec.Code)
	}
}

// --- Pages + static assets ------------------------------------------------

func TestPages(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "app.js"), []byte("console.log(1)"), 0o600); err != nil {
		t.Fatalf("write asset: %v", err)
	}
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.FrontendDir = dir })
	ts.reg.add("s1", "admin1", "public")

	for _, path := range []string{"/app/", "/app/connect", "/app/session/s1", "/app/operator/s1", "/app/replay/s1", "/app/inspect/s1"} {
		rec := ts.do("GET", path, "", adminHeaders())
		if rec.Code != http.StatusOK {
			t.Fatalf("page %s: %d", path, rec.Code)
		}
		if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "text/html") {
			t.Fatalf("page %s content-type: %q", path, ct)
		}
	}
	// Session page for an unreadable session → 403.
	ts.reg.add("priv", "admin1", "private")
	if rec := ts.do("GET", "/app/session/priv", "", viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("priv page: %d", rec.Code)
	}
	// Static asset served.
	if rec := ts.do("GET", "/_terminal/app.js", "", adminHeaders()); rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "console.log") {
		t.Fatalf("asset: %d %s", rec.Code, rec.Body.String())
	}
}

// --- Lifecycle + sweeps ---------------------------------------------------

func TestServerLifecycle(t *testing.T) {
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
		cfg.Server.Host = "127.0.0.1"
		cfg.Server.Port = 0
	})
	ctx, cancel := context.WithCancel(context.Background())
	errc := make(chan error, 1)
	go func() { errc <- ts.srv.Run(ctx) }()
	waitUntil(t, 5*time.Second, ts.srv.isReady)
	cancel()
	select {
	case err := <-errc:
		if err != nil {
			t.Fatalf("Run returned error: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Run did not return after cancel")
	}
}

func TestSweeps(t *testing.T) {
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
		cfg.SessionIdleTimeoutS = 3600
		cfg.SessionRetentionS = 1
	})
	ctx := context.Background()
	ts.setupWorker(t, "w1")
	// Stopped session past retention gets swept.
	ts.reg.add("old", "admin1", "public")
	stopped := ts.srv.clock.Wall() - 10
	ts.reg.statuses["old"].LifecycleState = "stopped"
	ts.reg.statuses["old"].StoppedAt = &stopped

	ts.srv.sweepApprovals(ctx)
	ts.srv.sweepIdleSessions(ctx)
	ts.srv.sweepExpiredSessions(ctx)
	if _, ok := ts.reg.GetDefinition(ctx, "old"); ok {
		t.Fatal("expired session not swept")
	}
	// Guards: disabled sweeps are no-ops.
	off := newTestServer(t, nil)
	off.srv.sweepIdleSessions(ctx)
	off.srv.sweepExpiredSessions(ctx)
}
