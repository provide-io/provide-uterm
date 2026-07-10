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
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// newWorkerTokenServer builds a Server whose hub requires a worker bearer token.
func newWorkerTokenServer(t *testing.T, token string) *Server {
	t.Helper()
	cfg := serverconfig.DefaultServerConfig()
	h := hub.NewTermHub(hub.TermHubConfig{WorkerToken: &token})
	reg := newFakeRegistry()
	srv, err := New(Deps{Hub: h, Auth: fakeAuth{}, Authz: serverauth.NewAuthorizationService(), Config: cfg, Registry: reg})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return srv
}

func TestWorkerWSTokenAuth(t *testing.T) {
	srv := newWorkerTokenServer(t, "sekret")
	httpSrv := httptest.NewServer(srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// Without the token → server accepts then closes 1008; a read fails.
	conn, _, err := websocket.Dial(ctx, wsBase+"/ws/worker/wx/term", nil)
	if err != nil {
		t.Fatalf("dial (no token): %v", err)
	}
	if _, _, rerr := conn.Read(ctx); rerr == nil {
		t.Fatal("expected close after missing token")
	}

	// With the correct token → the worker registers.
	conn2, _, err := websocket.Dial(ctx, wsBase+"/ws/worker/wx/term", &websocket.DialOptions{
		HTTPHeader: http.Header{"Authorization": {"Bearer sekret"}},
	})
	if err != nil {
		t.Fatalf("dial (token): %v", err)
	}
	defer func() { _ = conn2.Close(websocket.StatusNormalClosure, "") }()
	waitUntil(t, 5*time.Second, func() bool { return srv.deps.Hub.Registry.Contains("wx") })
}

func TestWorkerWSInvalidID(t *testing.T) {
	ts := newTestServer(t, nil)
	// A worker id violating the pattern → 404 (the mux matches, the handler
	// rejects). We assert via the HTTP handler that a non-upgrade GET 404s.
	rec := ts.do("GET", "/ws/worker/bad!id/term", "", nil)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("invalid worker id: %d", rec.Code)
	}
}

func TestWorkerWSProtocolMismatch(t *testing.T) {
	ts := newTestServer(t, nil)
	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	conn, _, err := websocket.Dial(ctx, wsBase+"/ws/worker/wm/term", nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	// A worker_hello advertising a protocol range the server does not support.
	hello, _ := encodeControlMap(map[string]any{
		"type": "worker_hello", "input_mode": "open",
		"protocol": map[string]any{"min": 99, "max": 99},
	})
	if err := conn.Write(ctx, websocket.MessageText, []byte(hello)); err != nil {
		t.Fatalf("write hello: %v", err)
	}
	// The server replies with an error frame then closes 1002; a subsequent read
	// eventually fails.
	sawError := false
	for i := 0; i < 4; i++ {
		mt, raw, rerr := conn.Read(ctx)
		if rerr != nil {
			break
		}
		if mt == websocket.MessageText && strings.Contains(string(raw), "protocol_mismatch") {
			sawError = true
		}
	}
	if !sawError {
		t.Fatal("expected protocol_mismatch error frame")
	}
}

func TestWorkerWSBadStream(t *testing.T) {
	ts := newTestServer(t, nil)
	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	conn, _, err := websocket.Dial(ctx, wsBase+"/ws/worker/wb/term", nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	// A malformed control frame (bad length header) → decoder error → 1003 close.
	if err := conn.Write(ctx, websocket.MessageText, []byte("\x10\x02zzzzzzzz:{}")); err != nil {
		t.Fatalf("write bad: %v", err)
	}
	// The worker receives a snapshot_req on connect before the close, so read
	// until the malformed-stream close arrives.
	closed := false
	for i := 0; i < 5; i++ {
		if _, _, rerr := conn.Read(ctx); rerr != nil {
			closed = true
			break
		}
	}
	if !closed {
		t.Fatal("expected close after malformed stream")
	}
}

func TestSessionListQuery(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("alpha", "admin1", "public")
	ts.reg.add("beta", "admin1", "public")
	ts.reg.statuses["alpha"].Tags = []string{"prod"}
	ts.reg.statuses["beta"].Tags = []string{"dev"}
	ts.reg.statuses["alpha"].DisplayName = "Alpha"
	ts.reg.statuses["beta"].DisplayName = "Beta"

	// Sort by display_name ascending.
	rec := ts.do("GET", "/api/sessions?sort=display_name&order=asc&limit=1&offset=0", "", adminHeaders())
	arr := decodeArray(t, rec.Body.Bytes())
	if len(arr) != 1 || arr[0].(map[string]any)["display_name"] != "Alpha" {
		t.Fatalf("sorted list: %s", rec.Body.String())
	}
	// Offset past the end → empty.
	rec = ts.do("GET", "/api/sessions?offset=50", "", adminHeaders())
	if len(decodeArray(t, rec.Body.Bytes())) != 0 {
		t.Fatalf("offset past end: %s", rec.Body.String())
	}
	// Tag filter.
	rec = ts.do("GET", "/api/sessions?tag=prod", "", adminHeaders())
	if a := decodeArray(t, rec.Body.Bytes()); len(a) != 1 {
		t.Fatalf("tag filter: %s", rec.Body.String())
	}
	// Free-text q filter.
	rec = ts.do("GET", "/api/sessions?q=beta", "", adminHeaders())
	if a := decodeArray(t, rec.Body.Bytes()); len(a) != 1 {
		t.Fatalf("q filter: %s", rec.Body.String())
	}
}

func TestBulkDeleteOlderThan(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("s1", "admin1", "public")
	stopped := ts.srv.clock.Wall() - 100
	ts.reg.statuses["s1"].LifecycleState = "stopped"
	ts.reg.statuses["s1"].StoppedAt = &stopped
	rec := ts.do("DELETE", "/api/sessions", `{"filter":{"state":"stopped","older_than_s":10}}`, adminHeaders())
	if rec.Code != http.StatusOK || decode(t, rec.Body.Bytes())["deleted"] != float64(1) {
		t.Fatalf("bulk older_than: %d %s", rec.Code, rec.Body.String())
	}
}
