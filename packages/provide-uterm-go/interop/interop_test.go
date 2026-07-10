//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

//go:build unix

// Live Go↔Python runtime interop proof. Unix-only: the clean process-group
// teardown (Setpgid + kill the whole `uv run` → python tree) relies on POSIX
// process groups. Both the CI runner (ubuntu) and dev (macOS) are unix.
package interop

import (
	"bytes"
	"context"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/client"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/defaults"
)

// referenceSessionID is the auto-started reference session both the Python and
// Go servers ship in their default config (UtermServerConfig.sessions). It is a
// "shell" connector in open input-mode with auto_start=true, so it exists the
// moment the server reports healthy — no session-creation dance required.
const referenceSessionID = "provide-shell"

// syncBuffer is a concurrency-safe buffer: the child process's output copier
// goroutine writes to it while the test goroutine reads it for diagnostics.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// repoRoot walks up from the test's working directory to the monorepo root
// (the dir containing packages/provide-uterm/src). Returns ok=false when it
// cannot be located, so the caller skips rather than fails.
func repoRoot() (string, bool) {
	root, err := os.Getwd()
	if err != nil {
		return "", false
	}
	for i := 0; i < 8; i++ {
		if _, err := os.Stat(filepath.Join(root, "packages", "provide-uterm", "src")); err == nil {
			return root, true
		}
		parent := filepath.Dir(root)
		if parent == root {
			return "", false
		}
		root = parent
	}
	return "", false
}

// freePort asks the OS for an ephemeral loopback port, then releases it so the
// Python server can bind it. The tiny reuse window is tolerated by the
// health-poll retry loop (a stolen port simply never reports healthy).
func freePort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", defaults.ServerHost+":0")
	if err != nil {
		t.Fatalf("allocate ephemeral port: %v", err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	if err := ln.Close(); err != nil {
		t.Fatalf("release ephemeral port: %v", err)
	}
	return port
}

// looksLikeMissingDeps reports whether a subprocess failure reads as "Python
// toolchain / deps not available" (→ skip) rather than "server is broken"
// (→ fail). Mirrors the conformance suite's "uv unavailable" graceful skip.
func looksLikeMissingDeps(log string) bool {
	needles := []string{
		"No module named",
		"ModuleNotFoundError",
		"ImportError",
		"Failed to spawn",
		"No such file or directory",
		"No solution found",
		"was not found",
	}
	for _, n := range needles {
		if strings.Contains(log, n) {
			return true
		}
	}
	return false
}

// pyServer is a running Python `uterm server` subprocess bound to an ephemeral
// loopback port with dev_token auth. The minted JWT is read back from the
// UTERM_DEV_TOKEN_PATH file the server writes at startup.
type pyServer struct {
	cmd     *exec.Cmd
	baseURL string
	wsBase  string
	token   string
	log     *syncBuffer
	// waitDone is closed (never sent-on) when cmd.Wait() returns, so both
	// waitHealthy's early-death check and stop()'s teardown wait can observe
	// completion independently — a data-carrying chan with a single buffered
	// slot would let one of the two drain it and leave the other blocked
	// forever on a channel nothing will ever send to again.
	waitDone chan struct{}
	waitErr  error
}

// startPyServer launches the server and blocks until it is healthy and has
// issued a dev token. It skips (never fails) when uv or the Python deps are
// unavailable, and registers its own teardown via t.Cleanup.
func startPyServer(t *testing.T, ctx context.Context) *pyServer {
	t.Helper()

	if _, err := exec.LookPath("uv"); err != nil {
		t.Skip("uv not on PATH; skipping live Python interop test")
	}
	root, ok := repoRoot()
	if !ok {
		t.Skip("monorepo root not found; skipping live Python interop test")
	}

	port := freePort(t)
	tokenPath := filepath.Join(t.TempDir(), "dev_token")
	logbuf := &syncBuffer{}

	// `uterm server` on loopback defaults to dev_token auth + the auto-started
	// provide-shell session. UTERM_API_ONLY=1 skips the frontend-asset check so
	// the headless test needs no built UI bundle; UTERM_DEV_TOKEN_PATH redirects
	// the minted JWT to a temp file we read back.
	cmd := exec.Command("uv", "run", "uterm", "server", //nolint:gosec // fixed command; host/port are test-controlled
		"--host", defaults.ServerHost, "--port", strconv.Itoa(port))
	cmd.Dir = root
	cmd.Env = append(os.Environ(),
		"UTERM_DEV_TOKEN_PATH="+tokenPath,
		"UTERM_API_ONLY=1",
	)
	cmd.Stdout = logbuf
	cmd.Stderr = logbuf
	// Own process group so teardown can reap the whole `uv run` → python tree.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	if err := cmd.Start(); err != nil {
		t.Skipf("could not start `uv run uterm server` (%v); skipping live interop", err)
	}

	srv := &pyServer{
		cmd:      cmd,
		baseURL:  fmt.Sprintf("http://%s:%d", defaults.ServerHost, port),
		wsBase:   fmt.Sprintf("ws://%s:%d", defaults.ServerHost, port),
		log:      logbuf,
		waitDone: make(chan struct{}),
	}
	go func() {
		srv.waitErr = cmd.Wait()
		close(srv.waitDone)
	}()
	t.Cleanup(srv.stop)

	srv.waitHealthy(t, ctx)
	srv.token = srv.readToken(t, ctx, tokenPath)
	return srv
}

// waitHealthy polls /api/health until it returns ok, or the process dies. On an
// early death that looks like a dependency gap it skips; otherwise it fails.
func (s *pyServer) waitHealthy(t *testing.T, ctx context.Context) {
	t.Helper()
	hc := client.NewHijackClient(s.baseURL, client.WithTimeout(3*time.Second))
	deadline := time.Now().Add(45 * time.Second)
	for time.Now().Before(deadline) {
		select {
		case <-s.waitDone:
			log := s.log.String()
			if looksLikeMissingDeps(log) {
				t.Skipf("Python server deps unavailable (exit: %v); skipping live interop\n%s", s.waitErr, tail(log))
			}
			t.Fatalf("Python server exited before healthy (%v):\n%s", s.waitErr, tail(log))
		default:
		}
		if h, err := hc.Health(ctx); err == nil && (h["status"] == "ok" || h["ok"] == true) {
			return
		}
		time.Sleep(250 * time.Millisecond)
	}
	t.Fatalf("Python server never became healthy within timeout:\n%s", tail(s.log.String()))
}

// readToken waits for the server to write the dev-token file, then returns it.
func (s *pyServer) readToken(t *testing.T, ctx context.Context, path string) string {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if raw, err := os.ReadFile(path); err == nil { //nolint:gosec // temp path under t.TempDir()
			if tok := strings.TrimSpace(string(raw)); tok != "" {
				return tok
			}
		}
		select {
		case <-ctx.Done():
			t.Fatalf("context cancelled waiting for dev token: %v", ctx.Err())
		case <-time.After(200 * time.Millisecond):
		}
	}
	t.Fatal("dev token file was never written by the Python server")
	return ""
}

// stop tears the server down: SIGTERM the process group, wait briefly, then
// SIGKILL as a fallback. Runs even on test failure (registered via t.Cleanup).
func (s *pyServer) stop() {
	if s.cmd.Process == nil {
		return
	}
	pgid := s.cmd.Process.Pid
	_ = syscall.Kill(-pgid, syscall.SIGTERM)
	select {
	case <-s.waitDone:
		return
	case <-time.After(5 * time.Second):
		_ = syscall.Kill(-pgid, syscall.SIGKILL)
		<-s.waitDone
	}
}

// authClient returns a HijackClient carrying the dev-token bearer header.
func (s *pyServer) authClient() *client.HijackClient {
	return client.NewHijackClient(s.baseURL,
		client.WithHeaders(map[string]string{"Authorization": "Bearer " + s.token}),
		client.WithTimeout(10*time.Second),
	)
}

// tail returns the last ~2KB of a log for compact failure output.
func tail(s string) string {
	const max = 2048
	if len(s) <= max {
		return s
	}
	return "…" + s[len(s)-max:]
}

// TestLivePythonInterop starts a real Python `uterm server`, then drives it
// from the Go client over the real wire, proving RUNTIME interop (not just the
// offline byte-comparison the conformance suite covers):
//
//   - REST: /api/health, /api/sessions, and the full operator hijack lease flow
//     (mode → acquire → send → snapshot → release) using client.HijackClient.
//   - WebSocket: the inline DLE/STX control channel — dial the browser control
//     WS with client.Dial, read the hello handshake frame, send an input frame,
//     and read the echoed terminal-data frames back.
//
// It asserts behavioral outcomes (session appears, sent marker echoes back),
// not merely that the process started.
func TestLivePythonInterop(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()

	srv := startPyServer(t, ctx)
	api := srv.authClient()

	// --- REST: authenticated health + session listing -------------------
	h, err := api.Health(ctx)
	if err != nil {
		t.Fatalf("authenticated Health: %v", err)
	}
	if h["status"] != "ok" && h["ok"] != true {
		t.Fatalf("health not ok: %v", h)
	}

	sessions, err := api.ListSessions(ctx)
	if err != nil {
		t.Fatalf("ListSessions: %v", err)
	}
	if !sessionListContains(sessions, referenceSessionID) {
		t.Fatalf("session %q not in listing: %v", referenceSessionID, sessions)
	}

	// --- REST: operator hijack lease round-trip -------------------------
	// The session auto-starts its worker link asynchronously after the server
	// reports healthy; wait for that link before driving the hijack flow.
	if !waitSessionConnected(t, ctx, api) {
		t.Fatalf("reference session worker never connected")
	}
	// The reference session ships in open mode; hijack requires exclusive mode.
	if _, err := api.SetSessionMode(ctx, referenceSessionID, "hijack"); err != nil {
		t.Fatalf("SetSessionMode(hijack): %v", err)
	}
	acq, err := acquireWithRetry(t, ctx, api)
	if err != nil {
		t.Fatalf("Acquire: %v", err)
	}
	hijackID, _ := acq["hijack_id"].(string)
	if hijackID == "" {
		t.Fatalf("acquire returned no hijack_id: %v", acq)
	}

	restMarker := fmt.Sprintf("rest-interop-%d", time.Now().UnixNano())
	if _, err := api.Send(ctx, referenceSessionID, hijackID, client.SendOptions{Keys: "echo " + restMarker + "\n"}); err != nil {
		t.Fatalf("Send: %v", err)
	}
	// The sent keys land in the hijack transcript; poll the snapshot for them.
	if !pollSnapshotFor(t, ctx, api, hijackID, restMarker) {
		t.Fatalf("sent marker %q never appeared in the hijack snapshot", restMarker)
	}
	if _, err := api.Release(ctx, referenceSessionID, hijackID); err != nil {
		t.Fatalf("Release: %v", err)
	}

	// --- WebSocket: inline control-channel input→echo round-trip --------
	// Back to open mode so a browser input frame reaches the connector directly.
	if _, err := api.SetSessionMode(ctx, referenceSessionID, "open"); err != nil {
		t.Fatalf("SetSessionMode(open): %v", err)
	}
	exerciseControlWS(t, ctx, srv)
}

// exerciseControlWS dials the browser control WS with the dev-token bearer,
// consumes the hello handshake frame, sends an input frame, and confirms the
// echoed terminal-data frames carry the marker — proving the live DLE/STX wire.
func exerciseControlWS(t *testing.T, ctx context.Context, srv *pyServer) {
	t.Helper()
	header := http.Header{}
	header.Set("Authorization", "Bearer "+srv.token)
	wsURL := fmt.Sprintf("%s/ws/browser/%s/term", srv.wsBase, referenceSessionID)

	dialCtx, dialCancel := context.WithTimeout(ctx, 10*time.Second)
	defer dialCancel()
	ws, err := client.Dial(dialCtx, wsURL, &client.DialOptions{Role: client.RoleBrowser, Headers: header})
	if err != nil {
		t.Fatalf("control-WS dial: %v", err)
	}
	defer func() { _ = ws.Close(websocket.StatusNormalClosure, "") }()

	rwCtx, rwCancel := context.WithTimeout(ctx, 15*time.Second)
	defer rwCancel()

	// The browser handshake opens with a hello control frame.
	if !waitForFrameType(ws, rwCtx, "hello") {
		t.Fatal("never received hello control frame over the WS")
	}

	wsMarker := fmt.Sprintf("ws-interop-%d", time.Now().UnixNano())
	if err := ws.SendFrame(rwCtx, map[string]any{"type": "input", "data": "echo " + wsMarker + "\n"}); err != nil {
		t.Fatalf("SendFrame(input): %v", err)
	}

	// The echo comes back over the live wire either as raw terminal-data
	// ("term" frames) or, for a screen-rendering connector like the reference
	// shell, inside a "snapshot" control frame's screen field. Accept either —
	// both are the Go client decoding real bytes the Python server wrote.
	var acc strings.Builder
	for {
		frame, err := ws.RecvFrame(rwCtx)
		if err != nil {
			t.Fatalf("RecvFrame while awaiting echo (%q): %v; got so far: %q", wsMarker, err, acc.String())
		}
		if data, ok := frame["data"].(string); ok {
			acc.WriteString(data)
		}
		if screen, ok := frame["screen"].(string); ok {
			acc.WriteString(screen)
		}
		if strings.Contains(acc.String(), wsMarker) {
			return // echo observed over the live control channel
		}
	}
}

// waitForFrameType reads frames until one of the given type arrives or the
// context deadline fires.
func waitForFrameType(ws *client.ControlWSClient, ctx context.Context, typ string) bool {
	for {
		frame, err := ws.RecvFrame(ctx)
		if err != nil {
			return false
		}
		if frame["type"] == typ {
			return true
		}
	}
}

// waitSessionConnected polls the session status until its worker link reports
// connected, so the subsequent hijack acquire does not race the auto-start.
func waitSessionConnected(t *testing.T, ctx context.Context, api *client.HijackClient) bool {
	t.Helper()
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		if m, err := api.GetSession(ctx, referenceSessionID); err == nil {
			if m["connected"] == true {
				return true
			}
		}
		time.Sleep(250 * time.Millisecond)
	}
	return false
}

// acquireWithRetry acquires a hijack lease, retrying briefly on the transient
// 409 ("No worker connected") that can occur right after a mode switch while
// the worker link settles.
func acquireWithRetry(t *testing.T, ctx context.Context, api *client.HijackClient) (map[string]any, error) {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	var lastErr error
	for time.Now().Before(deadline) {
		acq, err := api.Acquire(ctx, referenceSessionID, client.AcquireOptions{})
		if err == nil {
			return acq, nil
		}
		lastErr = err
		apiErr, ok := err.(*client.APIError)
		if !ok || apiErr.StatusCode != 409 {
			return nil, err
		}
		time.Sleep(300 * time.Millisecond)
	}
	return nil, lastErr
}

// pollSnapshotFor polls the hijack snapshot until its screen contains marker.
func pollSnapshotFor(t *testing.T, ctx context.Context, api *client.HijackClient, hijackID, marker string) bool {
	t.Helper()
	deadline := time.Now().Add(8 * time.Second)
	for time.Now().Before(deadline) {
		snap, err := api.Snapshot(ctx, referenceSessionID, hijackID, 0)
		if err == nil {
			if screen := snapshotScreen(snap); strings.Contains(screen, marker) {
				return true
			}
		}
		time.Sleep(250 * time.Millisecond)
	}
	return false
}

// snapshotScreen extracts the terminal screen text from a hijack snapshot body.
func snapshotScreen(snap map[string]any) string {
	inner, ok := snap["snapshot"].(map[string]any)
	if !ok {
		return ""
	}
	screen, _ := inner["screen"].(string)
	return screen
}

// sessionListContains reports whether the /api/sessions array holds a session
// with the given id.
func sessionListContains(sessions any, sessionID string) bool {
	arr, ok := sessions.([]any)
	if !ok {
		return false
	}
	for _, item := range arr {
		if m, ok := item.(map[string]any); ok {
			if m["session_id"] == sessionID {
				return true
			}
		}
	}
	return false
}
