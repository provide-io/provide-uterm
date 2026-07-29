//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"path/filepath"
	"testing"
	"time"
)

// The live-conformance server must honour auto_start, exactly as `uterm server`
// does. This is the path the cross-language matrix drives, and it used to be the
// only path with no boot step at all: StartAutoStartSessions was wired into
// runServer alone, so a server built through NewLiveServer reported the default
// session as never-started forever, while the reference and the C# port
// reported it running.
func TestLiveServerBringsUpAutoStartSessions(t *testing.T) {
	t.Setenv("UTERM_DEV_TOKEN_PATH", filepath.Join(t.TempDir(), "dev_token"))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	srv, err := NewLiveServer(ctx, ln, LiveServerOptions{AuthMode: "dev_token"})
	if err != nil {
		t.Fatalf("NewLiveServer: %v", err)
	}
	defer func() { _ = srv.Close() }()

	serveErr := make(chan error, 1)
	go func() { serveErr <- srv.Serve(ctx) }()

	sessions := listLiveSessions(t, srv)
	if len(sessions) != 1 {
		t.Fatalf("want the one configured session, got %d", len(sessions))
	}
	if got := sessions[0]["lifecycle_state"]; got != "running" {
		t.Fatalf("lifecycle_state = %#v, want \"running\" — the session is flagged auto_start", got)
	}
	if got := sessions[0]["auto_start"]; got != true {
		t.Fatalf("auto_start = %#v, want true", got)
	}
	if got := sessions[0]["last_error"]; got != nil {
		t.Fatalf("last_error = %#v, want null on a session that came up", got)
	}

	cancel()
	if err := <-serveErr; err != nil {
		t.Fatalf("Serve: %v", err)
	}
}

// listLiveSessions fetches GET /api/sessions from a running LiveServer,
// retrying while the socket is still coming up.
func listLiveSessions(t *testing.T, srv *LiveServer) []map[string]any {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	var lastErr error
	for time.Now().Before(deadline) {
		body, err := getLiveJSON(srv, "/api/sessions")
		if err == nil {
			var out []map[string]any
			if err := json.Unmarshal(body, &out); err != nil {
				t.Fatalf("decode /api/sessions: %v (body %s)", err, body)
			}
			return out
		}
		lastErr = err
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("GET /api/sessions never succeeded: %v", lastErr)
	return nil
}

// getLiveJSON performs one authenticated GET against a LiveServer.
func getLiveJSON(srv *LiveServer, path string) ([]byte, error) {
	req, err := http.NewRequest(http.MethodGet, srv.BaseURL()+path, nil)
	if err != nil {
		return nil, err
	}
	if token := srv.Token(); token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		return nil, &liveHTTPError{status: resp.StatusCode, body: string(body)}
	}
	return body, nil
}

// liveHTTPError reports a non-200 from the live server.
type liveHTTPError struct {
	status int
	body   string
}

func (e *liveHTTPError) Error() string {
	return "unexpected status " + http.StatusText(e.status) + ": " + e.body
}
