//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

//go:build unix

// Cross-language proxy-parity proof. A single observable fake upstream and a
// single Go WebSocket client drive BOTH the in-process Go `uterm proxy` and a
// live Python `uterm proxy` subprocess through the exact same behavioral
// contract (proxyEchoContract): banner flows remote→browser, keystrokes echo
// browser→remote→browser, and disconnects propagate in both directions.
//
// Unix-only: clean teardown of the `uv run` → python tree relies on POSIX
// process groups (Setpgid + kill the group). Both CI (ubuntu) and dev (macOS)
// are unix.
package cli

import (
	"context"
	"errors"
	"fmt"
	"net"
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

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/defaults"
)

// observableUpstream is a loopback TCP echo server that (a) writes a "READY"
// banner on accept, (b) echoes every byte it receives, and (c) exposes the
// lifecycle of its connections so a test can assert teardown propagation:
// closed fires whenever a connection's handler exits, and dropAll() lets the
// upstream initiate a close (simulating a remote hangup).
type observableUpstream struct {
	host   string
	port   int
	mu     sync.Mutex
	conns  []net.Conn
	closed chan struct{}
}

// startObservableUpstream binds an ephemeral loopback port and serves the echo
// protocol until the test ends.
func startObservableUpstream(t *testing.T) *observableUpstream {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen upstream: %v", err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	up := &observableUpstream{
		host:   "127.0.0.1",
		port:   ln.Addr().(*net.TCPAddr).Port,
		closed: make(chan struct{}, 16),
	}
	go up.acceptLoop(ln)
	return up
}

func (u *observableUpstream) acceptLoop(ln net.Listener) {
	for {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		u.mu.Lock()
		u.conns = append(u.conns, conn)
		u.mu.Unlock()
		go u.handle(conn)
	}
}

func (u *observableUpstream) handle(conn net.Conn) {
	defer func() {
		_ = conn.Close()
		u.remove(conn)
		select {
		case u.closed <- struct{}{}:
		default:
		}
	}()
	if _, err := conn.Write([]byte("READY")); err != nil {
		return
	}
	buf := make([]byte, 4096)
	for {
		n, err := conn.Read(buf)
		if n > 0 {
			if _, werr := conn.Write(buf[:n]); werr != nil {
				return
			}
		}
		if err != nil {
			return
		}
	}
}

func (u *observableUpstream) remove(conn net.Conn) {
	u.mu.Lock()
	defer u.mu.Unlock()
	for i, c := range u.conns {
		if c == conn {
			u.conns = append(u.conns[:i], u.conns[i+1:]...)
			return
		}
	}
}

// dropAll closes every live upstream connection, simulating a remote hangup.
func (u *observableUpstream) dropAll() {
	u.mu.Lock()
	conns := append([]net.Conn(nil), u.conns...)
	u.mu.Unlock()
	for _, c := range conns {
		_ = c.Close()
	}
}

// waitUpstreamClosed blocks until an upstream connection handler exits, proving
// the proxy tore the upstream down. Fails on timeout.
func (u *observableUpstream) waitUpstreamClosed(t *testing.T) {
	t.Helper()
	select {
	case <-u.closed:
	case <-time.After(4 * time.Second):
		t.Fatal("upstream connection was not closed after the browser disconnected")
	}
}

// dialWSWithRetry dials wsURL, retrying briefly to tolerate the window between a
// freshly-bound listener and its routes being ready (matters for the Python
// uvicorn subprocess). Returns a live connection or fails.
func dialWSWithRetry(t *testing.T, wsURL string) *websocket.Conn {
	t.Helper()
	deadline := time.Now().Add(8 * time.Second)
	var lastErr error
	for time.Now().Before(deadline) {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		conn, resp, err := websocket.Dial(ctx, wsURL, nil)
		cancel()
		if resp != nil && resp.Body != nil {
			_ = resp.Body.Close()
		}
		if err == nil {
			return conn
		}
		lastErr = err
		time.Sleep(150 * time.Millisecond)
	}
	t.Fatalf("ws dial %s never succeeded: %v", wsURL, lastErr)
	return nil
}

// expectWSClosed asserts the WS connection is closed by the server within a
// short window (draining any final flushed bytes first). A deadline without a
// close is a failure — the proxy failed to propagate the remote hangup.
func expectWSClosed(t *testing.T, conn *websocket.Conn) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	for {
		_, _, err := conn.Read(ctx)
		if err == nil {
			continue // final flushed bytes; keep reading until the close
		}
		if errors.Is(err, context.DeadlineExceeded) {
			t.Fatal("WS never closed after the upstream dropped the connection")
		}
		return // genuine close/read error — teardown propagated
	}
}

// proxyEchoContract is the single behavioral contract both the Go and Python
// proxies must honor against the same observable upstream. It is invoked
// identically for both implementations, so any divergence surfaces as a
// differential test failure.
func proxyEchoContract(t *testing.T, wsURL string, up *observableUpstream) {
	t.Helper()

	// (1) remote → browser: the upstream banner reaches the browser.
	conn := dialWSWithRetry(t, wsURL)
	readUntil(t, conn, []byte("READY"))

	// (2) browser → remote → browser: keystrokes echo back in order. The real
	// browser frontend sends TEXT frames (ws.send(string)); both proxies must
	// echo them back as TEXT. A multibyte UTF-8 payload proves valid UTF-8
	// round-trips byte-identically through both implementations.
	payload := []byte("PING café ▚")
	if err := conn.Write(context.Background(), websocket.MessageText, payload); err != nil {
		t.Fatalf("ws write: %v", err)
	}
	readUntil(t, conn, payload)

	// (3) browser disconnect propagates: closing the WS tears down the upstream.
	_ = conn.Close(websocket.StatusNormalClosure, "")
	up.waitUpstreamClosed(t)

	// (4) remote disconnect propagates: an upstream hangup closes the browser WS.
	conn2 := dialWSWithRetry(t, wsURL)
	defer conn2.CloseNow()               //nolint:errcheck // test cleanup
	readUntil(t, conn2, []byte("READY")) // ensure the upstream connection exists
	up.dropAll()
	expectWSClosed(t, conn2)
}

// TestProxyEchoContractGo drives the in-process Go proxy through the shared
// contract.
func TestProxyEchoContractGo(t *testing.T) {
	up := startObservableUpstream(t)

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen proxy: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	opts := proxyOptions{Host: up.host, BBSPort: up.port, Path: defaults.ProxyWSPath, Transport: "telnet"}
	serveErr := make(chan error, 1)
	go func() { serveErr <- serveProxy(ctx, ln, opts) }()

	wsURL := "ws://" + ln.Addr().String() + defaults.ProxyWSPath
	proxyEchoContract(t, wsURL, up)

	cancel()
	if err := <-serveErr; err != nil {
		t.Fatalf("serveProxy: %v", err)
	}
}

// TestProxyEchoContractPython drives a live Python `uterm proxy` subprocess
// through the exact same shared contract, proving byte-for-byte parity of the
// observable behavior across the two implementations. It skips (never fails)
// when uv or the Python deps are unavailable.
func TestProxyEchoContractPython(t *testing.T) {
	up := startObservableUpstream(t)
	proxy := startPyProxy(t, up.host, up.port)
	proxyEchoContract(t, proxy.wsURL, up)
}

// pyProxy is a running Python `uterm proxy` subprocess bound to an ephemeral
// loopback port.
type pyProxy struct {
	cmd    *exec.Cmd
	wsURL  string
	log    *proxyLogBuf
	exited chan error
}

// startPyProxy launches `uv run uterm proxy <host> <port> ...` against the given
// upstream and blocks until it is accepting connections. Skips when uv or the
// monorepo root is unavailable; registers its own teardown.
func startPyProxy(t *testing.T, upstreamHost string, upstreamPort int) *pyProxy {
	t.Helper()

	if _, err := exec.LookPath("uv"); err != nil {
		t.Skip("uv not on PATH; skipping live Python proxy-parity test")
	}
	root, ok := proxyRepoRoot()
	if !ok {
		t.Skip("monorepo root not found; skipping live Python proxy-parity test")
	}

	proxyPort := proxyFreePort(t)
	logbuf := &proxyLogBuf{}

	cmd := exec.Command("uv", "run", "uterm", "proxy", //nolint:gosec // fixed command; host/port are test-controlled
		upstreamHost, strconv.Itoa(upstreamPort),
		"--bind", "127.0.0.1",
		"--port", strconv.Itoa(proxyPort),
		"--path", defaults.ProxyWSPath,
		"--transport", "telnet",
	)
	cmd.Dir = root
	cmd.Stdout = logbuf
	cmd.Stderr = logbuf
	// Own process group so teardown can reap the whole `uv run` → python tree.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	if err := cmd.Start(); err != nil {
		t.Skipf("could not start `uv run uterm proxy` (%v); skipping live parity", err)
	}

	p := &pyProxy{
		cmd:    cmd,
		wsURL:  fmt.Sprintf("ws://127.0.0.1:%d%s", proxyPort, defaults.ProxyWSPath),
		log:    logbuf,
		exited: make(chan error, 1),
	}
	go func() { p.exited <- cmd.Wait() }()
	t.Cleanup(p.stop)

	p.waitListening(t, proxyPort)
	return p
}

// waitListening polls a TCP connect to the proxy port until it succeeds or the
// process dies. An early death that reads as a dependency gap skips; otherwise
// it fails.
func (p *pyProxy) waitListening(t *testing.T, port int) {
	t.Helper()
	addr := net.JoinHostPort("127.0.0.1", strconv.Itoa(port))
	deadline := time.Now().Add(45 * time.Second)
	for time.Now().Before(deadline) {
		select {
		case err := <-p.exited:
			log := p.log.String()
			if looksLikeMissingProxyDeps(log) {
				t.Skipf("Python proxy deps unavailable (exit: %v); skipping live parity\n%s", err, proxyTail(log))
			}
			t.Fatalf("Python proxy exited before listening (%v):\n%s", err, proxyTail(log))
		default:
		}
		conn, err := net.DialTimeout("tcp", addr, 500*time.Millisecond)
		if err == nil {
			_ = conn.Close()
			return
		}
		time.Sleep(200 * time.Millisecond)
	}
	t.Fatalf("Python proxy never started listening within timeout:\n%s", proxyTail(p.log.String()))
}

// stop tears the proxy down: SIGTERM the process group, then SIGKILL as a
// fallback. Runs even on failure (registered via t.Cleanup).
func (p *pyProxy) stop() {
	if p.cmd.Process == nil {
		return
	}
	pgid := p.cmd.Process.Pid
	_ = syscall.Kill(-pgid, syscall.SIGTERM)
	select {
	case <-p.exited:
		return
	case <-time.After(5 * time.Second):
		_ = syscall.Kill(-pgid, syscall.SIGKILL)
		<-p.exited
	}
}

// proxyLogBuf is a concurrency-safe buffer for capturing subprocess output.
type proxyLogBuf struct {
	mu  sync.Mutex
	buf strings.Builder
}

func (b *proxyLogBuf) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *proxyLogBuf) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// proxyRepoRoot walks up to the monorepo root (dir containing
// packages/provide-uterm/src). Returns ok=false when it cannot be located.
func proxyRepoRoot() (string, bool) {
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

// proxyFreePort asks the OS for an ephemeral loopback port, then releases it so
// the Python subprocess can bind it.
func proxyFreePort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("allocate ephemeral port: %v", err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	if err := ln.Close(); err != nil {
		t.Fatalf("release ephemeral port: %v", err)
	}
	return port
}

// looksLikeMissingProxyDeps reports whether a subprocess failure reads as a
// Python toolchain/dependency gap (→ skip) rather than a real defect (→ fail).
func looksLikeMissingProxyDeps(log string) bool {
	needles := []string{
		"No module named",
		"ModuleNotFoundError",
		"ImportError",
		"missing dependency",
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

// proxyTail returns the last ~2000 bytes of a log for diagnostics.
func proxyTail(s string) string {
	const max = 2000
	if len(s) <= max {
		return s
	}
	return "…" + s[len(s)-max:]
}
