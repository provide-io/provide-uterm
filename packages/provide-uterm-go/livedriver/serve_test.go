//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package livedriver

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// isolateDevTokenFor keeps a test's dev-IdP token out of the developer's home.
func isolateDevTokenFor(t *testing.T) {
	t.Helper()
	t.Setenv("UTERM_DEV_TOKEN_PATH", filepath.Join(t.TempDir(), "dev_token"))
}

// startServe runs RunServe in the background and returns the parsed handshake
// line plus a stop function that closes stdin and waits for a clean shutdown.
func startServe(t *testing.T, opts ServeOptions) (ServerLine, func()) {
	t.Helper()
	isolateDevTokenFor(t)

	stdinR, stdinW := io.Pipe()
	stdoutR, stdoutW := io.Pipe()
	done := make(chan error, 1)
	go func() { done <- RunServe(context.Background(), opts, stdinR, stdoutW) }()

	lineCh := make(chan string, 1)
	go func() {
		line, _ := bufio.NewReader(stdoutR).ReadString('\n')
		lineCh <- line
	}()

	var raw string
	select {
	case raw = <-lineCh:
	case err := <-done:
		t.Fatalf("RunServe exited before writing its handshake: %v", err)
	case <-time.After(30 * time.Second):
		t.Fatal("timed out waiting for the handshake line")
	}

	var line ServerLine
	if err := json.Unmarshal([]byte(raw), &line); err != nil {
		t.Fatalf("handshake %q: %v", raw, err)
	}
	stop := func() {
		_ = stdinW.Close()
		select {
		case err := <-done:
			if err != nil {
				t.Errorf("RunServe: %v", err)
			}
		case <-time.After(30 * time.Second):
			t.Error("RunServe did not shut down after stdin closed")
		}
		_ = stdoutR.Close()
		_ = stdoutW.Close()
	}
	return line, stop
}

func TestRunServeHandshakeAndShutdown(t *testing.T) {
	line, stop := startServe(t, ServeOptions{})
	defer stop()

	if line.Role != RoleServer || line.Language != Language {
		t.Fatalf("handshake role/language = %+v", line)
	}
	if !strings.HasPrefix(line.BaseURL, "http://127.0.0.1:") {
		t.Fatalf("base_url = %q, want a loopback origin", line.BaseURL)
	}
	// The port must be whatever the OS handed out, never 0 and never named.
	if strings.HasSuffix(line.BaseURL, ":0") {
		t.Fatalf("base_url reports the bind sentinel, not the real port: %q", line.BaseURL)
	}
	if line.Token == "" {
		t.Fatal("dev_token mode must report a presentable token")
	}
	if len(line.Capabilities) != len(Capabilities()) {
		t.Fatalf("capabilities = %v", line.Capabilities)
	}
}

func TestRunServeServesTheGoServer(t *testing.T) {
	line, stop := startServe(t, ServeOptions{AuthMode: "dev_token"})
	defer stop()

	// Anonymous health, then an authenticated route with each auth selector —
	// the driver's own client path, end to end against the real server.
	sc := &Scenario{ID: "010_live", TimeoutMS: 20000, Steps: []Step{
		{ID: "health", Action: ActionHealth},
		{ID: "sessions", Action: ActionListSessions},
		{ID: "sessions_none", Action: ActionListSessions, Auth: AuthNone},
		{ID: "sessions_bad", Action: ActionListSessions, Auth: AuthBad},
	}}
	r := RunScenario(context.Background(), sc, ClientOptions{BaseURL: line.BaseURL, Token: line.Token})
	if r.Status != StatusCompleted {
		t.Fatalf("status = %s (%v)", r.Status, r.Error)
	}
	byID := map[string]StepFields{}
	for _, s := range r.Steps {
		byID[s.ID] = s.Fields
	}
	if f := byID["health"]; f.Status == nil || *f.Status != http.StatusOK || !f.OK {
		t.Fatalf("health = %+v", f)
	}
	if f := byID["sessions"]; f.Status == nil || *f.Status != http.StatusOK || !f.OK {
		t.Fatalf("authenticated sessions = %+v", f)
	}
	for _, id := range []string{"sessions_none", "sessions_bad"} {
		f := byID[id]
		if f.Status == nil || f.OK {
			t.Fatalf("%s should have been refused, got %+v", id, f)
		}
		if *f.Status < 400 || *f.Status > 499 {
			t.Fatalf("%s status = %d, want a 4xx refusal", id, *f.Status)
		}
	}
}

func TestRunServeRejectsABadConfig(t *testing.T) {
	isolateDevTokenFor(t)
	err := RunServe(context.Background(), ServeOptions{
		ConfigPath: filepath.Join(t.TempDir(), "no-such-config.toml"),
	}, nil, io.Discard)
	if err == nil {
		t.Fatal("expected a config load error")
	}
}

func TestRunServeSurfacesAHandshakeWriteFailure(t *testing.T) {
	isolateDevTokenFor(t)
	// stdin nil so no shutdown watcher runs; the failure must return before
	// the server ever starts serving, in both the write and the flush.
	if err := RunServe(context.Background(), ServeOptions{}, nil, errWriter{}); err == nil {
		t.Fatal("expected the handshake write failure to surface")
	}
	if err := RunServe(context.Background(), ServeOptions{}, nil, failFlusher{}); err == nil {
		t.Fatal("expected the handshake flush failure to surface")
	}
}

func TestRunServeStopsWhenTheContextIsCancelled(t *testing.T) {
	isolateDevTokenFor(t)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	out := &syncWriter{}
	go func() { done <- RunServe(ctx, ServeOptions{}, nil, out) }()

	deadline := time.After(30 * time.Second)
	for !strings.Contains(out.String(), "base_url") {
		select {
		case err := <-done:
			t.Fatalf("RunServe exited early: %v", err)
		case <-deadline:
			t.Fatal("timed out waiting for the handshake")
		case <-time.After(20 * time.Millisecond):
		}
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("RunServe: %v", err)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("RunServe ignored the cancelled context")
	}
}

func TestServeOptionsAuthMode(t *testing.T) {
	if got := (ServeOptions{}).authMode(); got != DefaultAuthMode {
		t.Fatalf("default auth mode = %q", got)
	}
	if got := (ServeOptions{AuthMode: "  "}).authMode(); got != DefaultAuthMode {
		t.Fatalf("blank auth mode = %q", got)
	}
	if got := (ServeOptions{AuthMode: " jwt "}).authMode(); got != "jwt" {
		t.Fatalf("explicit auth mode = %q", got)
	}
}

func TestFlush(t *testing.T) {
	var sb strings.Builder
	if err := flush(&sb); err != nil {
		t.Fatalf("an unbuffered writer needs no flush: %v", err)
	}
	bw := bufio.NewWriter(&sb)
	if _, err := bw.WriteString("x"); err != nil {
		t.Fatalf("write: %v", err)
	}
	if err := flush(bw); err != nil {
		t.Fatalf("flush: %v", err)
	}
	if sb.String() != "x" {
		t.Fatalf("buffered writer was not flushed: %q", sb.String())
	}
	if err := flush(failFlusher{}); err == nil {
		t.Fatal("expected the flusher's error to surface")
	}
}

// failFlusher reports a flush failure.
type failFlusher struct{}

func (failFlusher) Write(p []byte) (int, error) { return len(p), nil }
func (failFlusher) Flush() error                { return errors.New("flush failed") }

// syncWriter serialises writes so a test goroutine can poll what was written.
type syncWriter struct {
	mu  sync.Mutex
	buf strings.Builder
}

func (s *syncWriter) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.buf.Write(p)
}

func (s *syncWriter) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.buf.String()
}
