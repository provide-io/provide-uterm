//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bytes"
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/transports"
)

// startEchoServer starts a loopback TCP server that writes a banner then echoes
// every byte it receives. It returns host and port.
func startEchoServer(t *testing.T, banner []byte) (string, int) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer func() { _ = c.Close() }()
				if len(banner) > 0 {
					_, _ = c.Write(banner)
				}
				buf := make([]byte, 4096)
				for {
					n, err := c.Read(buf)
					if n > 0 {
						_, _ = c.Write(buf[:n])
					}
					if err != nil {
						return
					}
				}
			}(conn)
		}
	}()
	addr := ln.Addr().(*net.TCPAddr)
	return "127.0.0.1", addr.Port
}

// readUntil reads ws frames until the accumulated bytes contain want, or fails.
func readUntil(t *testing.T, conn *websocket.Conn, want []byte) {
	t.Helper()
	var acc bytes.Buffer
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
		_, data, err := conn.Read(ctx)
		cancel()
		if err != nil {
			continue
		}
		acc.Write(data)
		if bytes.Contains(acc.Bytes(), want) {
			return
		}
	}
	t.Fatalf("never saw %q; got %q", want, acc.Bytes())
}

func TestProxyBidirectional(t *testing.T) {
	host, port := startEchoServer(t, []byte("READY"))

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	opts := proxyOptions{Host: host, BBSPort: port, Path: "/ws/terminal", Transport: "telnet"}
	serveErr := make(chan error, 1)
	go func() { serveErr <- serveProxy(ctx, ln, opts) }()

	dialCtx, dialCancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer dialCancel()
	conn, resp, err := websocket.Dial(dialCtx, "ws://"+ln.Addr().String()+"/ws/terminal", nil)
	if resp != nil && resp.Body != nil {
		_ = resp.Body.Close()
	}
	if err != nil {
		t.Fatalf("ws dial: %v", err)
	}
	defer conn.CloseNow() //nolint:errcheck // test cleanup

	// remote → browser: the banner flows out.
	readUntil(t, conn, []byte("READY"))

	// browser → remote → browser: keystrokes echo back.
	if err := conn.Write(context.Background(), websocket.MessageBinary, []byte("PING")); err != nil {
		t.Fatalf("ws write: %v", err)
	}
	readUntil(t, conn, []byte("PING"))

	_ = conn.Close(websocket.StatusNormalClosure, "")
	cancel()
	if err := <-serveErr; err != nil {
		t.Fatalf("serveProxy: %v", err)
	}
}

func TestProxyConnectFailure(t *testing.T) {
	// Point at a port that nothing is listening on.
	dead, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	deadPort := dead.Addr().(*net.TCPAddr).Port
	_ = dead.Close()

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	opts := proxyOptions{Host: "127.0.0.1", BBSPort: deadPort, Path: "/ws/terminal", Transport: "telnet"}
	go func() { _ = serveProxy(ctx, ln, opts) }()

	dialCtx, dialCancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer dialCancel()
	conn, resp, err := websocket.Dial(dialCtx, "ws://"+ln.Addr().String()+"/ws/terminal", nil)
	if resp != nil && resp.Body != nil {
		_ = resp.Body.Close()
	}
	if err != nil {
		// A failed upgrade is also an acceptable manifestation of the failure.
		return
	}
	defer conn.CloseNow() //nolint:errcheck // test cleanup
	// The upstream connect failed → the server closes the socket; a Read errors.
	readCtx, readCancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer readCancel()
	if _, _, err := conn.Read(readCtx); err == nil {
		t.Fatal("expected read error after upstream connect failure")
	}
}

func TestNewProxyTransport(t *testing.T) {
	if _, ok := newProxyTransport("telnet").(*transports.TelnetTransport); !ok {
		t.Fatal("telnet kind should build a TelnetTransport")
	}
	if _, ok := newProxyTransport("ssh").(*transports.SSHTransport); !ok {
		t.Fatal("ssh kind should build an SSHTransport")
	}
}

func TestProxyArgErrors(t *testing.T) {
	var out, errw bytes.Buffer
	if code := Execute([]string{"proxy", "host", "notaport"}, &out, &errw); code == 0 {
		t.Fatal("non-integer PORT should fail")
	}
	out.Reset()
	errw.Reset()
	if code := Execute([]string{"proxy", "host", "23", "--transport", "bogus"}, &out, &errw); code == 0 {
		t.Fatal("bogus transport should fail")
	}
}

func TestServeProxyListenerError(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	_ = ln.Close() // serving on a closed listener surfaces a non-graceful error
	opts := proxyOptions{Host: "127.0.0.1", BBSPort: 1, Path: "/ws", Transport: "telnet"}
	if err := serveProxy(context.Background(), ln, opts); err == nil {
		t.Fatal("expected serveProxy error on closed listener")
	}
}

func TestRunProxyBindError(t *testing.T) {
	// A nil context also exercises the background-context fallback branch.
	err := runProxy(nil, proxyOptions{Bind: "300.300.300.300", Port: 0, Path: "/ws", Transport: "telnet"}) //nolint:staticcheck // nil ctx intentional
	if err == nil {
		t.Fatal("expected bind error for invalid address")
	}
}

func TestProxyHandlerAcceptFailure(t *testing.T) {
	srv := httptest.NewServer(proxyHandler(proxyOptions{Host: "127.0.0.1", BBSPort: 1, Path: "/ws/terminal", Transport: "telnet"}))
	defer srv.Close()
	// A plain GET (no WebSocket upgrade) makes websocket.Accept fail; the
	// handler must return without dialing upstream.
	resp, err := http.Get(srv.URL + "/ws/terminal") //nolint:noctx // test
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	_ = resp.Body.Close()
}
