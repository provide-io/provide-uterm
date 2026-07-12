//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// demo-recording-http: thin server recording surface demo (Go).
// Starts an in-process uterm server, seeds a LocalFileStore session, then
// exercises POST annotate + GET recording / entries / download — same HTTP
// contract as Python routes/sessions.py and the C# UtermServer port.
package main

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

const (
	magenta = "\033[1;35m"
	green   = "\033[1;32m"
	cyan    = "\033[1;36m"
	dim     = "\033[2m"
	reset   = "\033[0m"
	bold    = "\033[1m"
)

func banner(title string) {
	bar := strings.Repeat("═", len(title)+4)
	fmt.Printf("\n%s%s%s\n", magenta, bar, reset)
	fmt.Printf("%s  %s%s%s  %s\n", magenta, bold, title, reset+magenta, reset)
	fmt.Printf("%s%s%s\n\n", magenta, bar, reset)
}

func info(msg string) { fmt.Printf("%s  → %s%s\n", cyan, msg, reset) }
func ok(msg string)   { fmt.Printf("%s  ✓ %s%s\n", green, msg, reset) }
func kv(k string, v any) {
	fmt.Printf("    %s%s:%s %s%v%s\n", dim, k, reset, bold, v, reset)
}

// simpleRegistry is a minimal SessionRegistry for the demo (one public session).
type simpleRegistry struct {
	def *serverconfig.SessionDefinition
	st  *server.SessionStatus
}

func (r *simpleRegistry) GetDefinition(_ context.Context, id string) (*serverconfig.SessionDefinition, bool) {
	if id != r.def.SessionID {
		return nil, false
	}
	return r.def, true
}
func (r *simpleRegistry) ListWithDefinitions(context.Context) []server.SessionListItem {
	return []server.SessionListItem{{Status: r.st, Definition: r.def}}
}
func (r *simpleRegistry) GetSession(_ context.Context, id string) (*server.SessionStatus, error) {
	if id != r.def.SessionID {
		return nil, server.ErrSessionNotFound
	}
	return r.st, nil
}
func (r *simpleRegistry) CreateSession(context.Context, map[string]any) (*server.SessionStatus, error) {
	return nil, fmt.Errorf("not implemented")
}
func (r *simpleRegistry) CreateSessionInternal(ctx context.Context, payload map[string]any) (*server.SessionStatus, error) {
	return r.CreateSession(ctx, payload)
}
func (r *simpleRegistry) UpdateSession(context.Context, string, map[string]any) (*server.SessionStatus, error) {
	return nil, fmt.Errorf("not implemented")
}
func (r *simpleRegistry) DeleteSession(context.Context, string) error { return nil }
func (r *simpleRegistry) StartSession(context.Context, string) (*server.SessionStatus, error) {
	return r.st, nil
}
func (r *simpleRegistry) StopSession(context.Context, string) (*server.SessionStatus, error) {
	return r.st, nil
}
func (r *simpleRegistry) RestartSession(context.Context, string) (*server.SessionStatus, error) {
	return r.st, nil
}
func (r *simpleRegistry) ClearSession(context.Context, string) (*server.SessionStatus, error) {
	return r.st, nil
}
func (r *simpleRegistry) SetMode(context.Context, string, string) (*server.SessionStatus, error) {
	return r.st, nil
}
func (r *simpleRegistry) AnalyzeSession(context.Context, string) (map[string]any, error) {
	return map[string]any{}, nil
}
func (r *simpleRegistry) LastSnapshot(context.Context, string) (map[string]any, error) {
	return nil, nil
}
func (r *simpleRegistry) Events(context.Context, string, int) ([]map[string]any, error) {
	return nil, nil
}
func (r *simpleRegistry) WatchSessionEvents(context.Context, string, server.WatchParams) (map[string]any, error) {
	return map[string]any{}, nil
}
func (r *simpleRegistry) AnnotateSession(_ context.Context, _ string, _ server.Annotation) (float64, int, error) {
	return float64(time.Now().UnixNano()) / 1e9, 1, nil
}

func main() {
	banner("provide-uterm recording HTTP — Go")
	info("surface=annotate+meta+entries+download  store=LocalFileStore")

	tmp, err := os.MkdirTemp("", "uterm-rec-http-go-*")
	if err != nil {
		fmt.Fprintf(os.Stderr, "temp dir: %v\n", err)
		os.Exit(1)
	}
	defer os.RemoveAll(tmp)

	const sid = "demo-http-go"
	store := recording.NewLocalFileStore(tmp)
	_ = store.StartSession(sid, map[string]any{
		"lang": "go", "feature": "recording_http", "demo": "thin_server_surface",
	})
	_ = store.AppendEvents(sid, []recording.Event{
		{"ts": 1.0, "event": "snapshot", "data": map[string]any{"screen": "=== recording HTTP demo ===\n"}, "session_id": sid},
		{"ts": 2.0, "event": "output", "data": "hello from go\n", "session_id": sid},
	})
	ok("seeded JSONL under " + tmp)

	owner := "demo-admin"
	cfg := serverconfig.DefaultServerConfig()
	cfg.Auth.Mode = "header"
	cfg.Auth.HeaderModeAcknowledged = true
	cfg.Auth.TrustedProxyIPs = []string{"127.0.0.1", "::1"}
	cfg.Recording.StoreType = "local"
	cfg.Recording.Directory = tmp
	cfg.Server.Host = "127.0.0.1"
	cfg.Server.Port = 0

	reg := &simpleRegistry{
		def: &serverconfig.SessionDefinition{
			SessionID: sid, DisplayName: sid, ConnectorType: "shell",
			Visibility: "public", Owner: &owner,
		},
		st: &server.SessionStatus{
			SessionID: sid, DisplayName: sid, ConnectorType: "shell",
			LifecycleState: "running", InputMode: "hijack", Visibility: "public",
			Owner: &owner, Tags: []string{},
		},
	}
	quiet := slog.New(slog.NewTextHandler(io.Discard, nil))
	clock := hub.NewRealClock()
	h := hub.NewTermHub(hub.TermHubConfig{Clock: clock, Logger: quiet})
	authz := serverauth.NewAuthorizationService()
	auth := serverauth.NewLocalIdentityProvider(&cfg.Auth, serverauth.NewApiKeyStore())

	srv, err := server.New(server.Deps{
		Hub: h, Auth: auth, Authz: authz, Config: cfg, Registry: reg,
		APIKeys: serverauth.NewApiKeyStore(), Metrics: server.NewMetrics(),
		Clock: clock, Version: "demo", Logger: quiet, Recording: store,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "server.New: %v\n", err)
		os.Exit(1)
	}

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		fmt.Fprintf(os.Stderr, "listen: %v\n", err)
		os.Exit(1)
	}
	base := "http://" + ln.Addr().String()
	httpSrv := &http.Server{Handler: srv.Handler()}
	go func() { _ = httpSrv.Serve(ln) }()
	defer func() { _ = httpSrv.Close() }()
	ok("server listening " + base)

	client := &http.Client{Timeout: 5 * time.Second}
	do := func(method, path, body string) (int, string) {
		var rdr io.Reader
		if body != "" {
			rdr = strings.NewReader(body)
		}
		req, _ := http.NewRequest(method, base+path, rdr)
		req.Header.Set("X-Uterm-Principal", owner)
		req.Header.Set("X-Uterm-Role", "admin")
		if body != "" {
			req.Header.Set("Content-Type", "application/json")
		}
		resp, err := client.Do(req)
		if err != nil {
			return 0, err.Error()
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		return resp.StatusCode, string(b)
	}

	code, body := do("GET", "/api/sessions/"+sid+"/recording", "")
	kv("GET /recording", fmt.Sprintf("%d %s", code, trim(body, 120)))
	if code != 200 {
		os.Exit(1)
	}
	ok("recording meta")

	code, body = do("GET", "/api/sessions/"+sid+"/recording/entries?limit=10", "")
	kv("GET /recording/entries", fmt.Sprintf("%d bytes=%d", code, len(body)))
	if code != 200 {
		os.Exit(1)
	}
	ok("recording entries")

	code, body = do("POST", "/api/sessions/"+sid+"/annotate",
		`{"label":"http-demo","description":"thin surface","severity":"info"}`)
	kv("POST /annotate", fmt.Sprintf("%d %s", code, trim(body, 80)))
	if code != 200 {
		os.Exit(1)
	}
	ok("annotate")

	code, body = do("GET", "/api/sessions/"+sid+"/recording/download", "")
	kv("GET /recording/download", fmt.Sprintf("%d bytes=%d", code, len(body)))
	if code != 200 {
		os.Exit(1)
	}
	// show first line of JSONL
	line := strings.SplitN(body, "\n", 2)[0]
	kv("jsonl[0]", trim(line, 100))
	ok("download JSONL from " + filepath.Join(tmp, sid+".jsonl"))

	fmt.Println()
	ok("thin recording HTTP surface demo complete (Go)")
}

func trim(s string, n int) string {
	s = strings.ReplaceAll(s, "\n", " ")
	if len(s) > n {
		return s[:n] + "…"
	}
	return s
}
