//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package gateway

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"
	"golang.org/x/crypto/ssh"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/colors"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

// scanFor reads from r until substr is seen or timeout elapses.
func scanFor(t *testing.T, r io.Reader, substr string, timeout time.Duration) {
	t.Helper()
	found := make(chan struct{})
	go func() {
		var acc []byte
		tmp := make([]byte, 256)
		for {
			n, err := r.Read(tmp)
			acc = append(acc, tmp[:n]...)
			if bytes.Contains(acc, []byte(substr)) {
				close(found)
				return
			}
			if err != nil {
				return
			}
		}
	}()
	select {
	case <-found:
	case <-time.After(timeout):
		t.Fatalf("did not observe %q within %v", substr, timeout)
	}
}

// echoWSChunks reads text messages, echoing every data chunk and reporting each
// control frame's type on ctrl (best-effort, non-blocking). Reusable upstream
// body for scripted-server tests.
func echoWSChunks(ctx context.Context, conn *websocket.Conn, ctrl chan<- string) {
	dec := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
	for {
		typ, msg, err := conn.Read(ctx)
		if err != nil {
			return
		}
		if typ != websocket.MessageText {
			continue
		}
		events, _ := dec.Feed(string(msg))
		for _, ev := range events {
			switch e := ev.(type) {
			case controlchannel.ControlChunk:
				if ctrl != nil {
					if ct, ok := e.Control["type"].(string); ok {
						select {
						case ctrl <- ct:
						default:
						}
					}
				}
			case controlchannel.DataChunk:
				_ = conn.Write(ctx, websocket.MessageText, []byte(controlchannel.EncodeTerminalData(e.Data)))
			}
		}
	}
}

// startScriptedWS starts an upstream WS server whose per-request behavior is
// chosen by URL path, so a single origin can host a redirect target plus a
// scripted first hop. handlers is keyed by path (e.g. "/ws").
func startScriptedWS(t *testing.T, handlers map[string]func(ctx context.Context, conn *websocket.Conn)) string {
	t.Helper()
	mux := http.NewServeMux()
	for path, h := range handlers {
		h := h
		mux.HandleFunc(path, func(w http.ResponseWriter, r *http.Request) {
			conn, err := websocket.Accept(w, r, nil)
			if err != nil {
				return
			}
			conn.SetReadLimit(-1)
			h(r.Context(), conn)
		})
	}
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return "ws://" + strings.TrimPrefix(srv.URL, "http://")
}

// dialTelnet dials the gateway listener and registers cleanup.
func dialTelnet(t *testing.T, ln net.Listener) net.Conn {
	t.Helper()
	conn, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return conn
}

// expectClosed asserts the connection reaches EOF (or any read error) within a
// short window — used when the gateway is expected to tear the client down.
func expectClosed(t *testing.T, conn net.Conn) {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	buf := make([]byte, 256)
	for {
		if _, err := conn.Read(buf); err != nil {
			return
		}
	}
}

// TestTelnetGatewayUpstreamDialFailure drives the pumpOnce dial-error path: the
// upstream WS URL refuses connections, so with MaxReconnects=0 the gateway
// exhausts its attempts and closes the client cleanly.
func TestTelnetGatewayUpstreamDialFailure(t *testing.T) {
	// Reserve then release a port so the address is refused, not just unrouted.
	probe, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	deadAddr := probe.Addr().String()
	_ = probe.Close()

	gw := &TelnetWsGateway{WSURL: "ws://" + deadAddr + "/ws", MaxReconnects: 0}
	ln, err := gw.Start("127.0.0.1", 0)
	if err != nil {
		t.Fatalf("start: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go gw.Serve(ctx, ln) //nolint:errcheck

	conn := dialTelnet(t, ln)
	expectClosed(t, conn)
}

// TestTelnetGatewayBinaryFrame covers pumpWSToClient's binary-message branch:
// the upstream sends a binary WS message, which the gateway forwards to the
// telnet client through the write transform.
func TestTelnetGatewayBinaryFrame(t *testing.T) {
	wsURL := startScriptedWS(t, map[string]func(context.Context, *websocket.Conn){
		"/ws": func(ctx context.Context, conn *websocket.Conn) {
			_ = conn.Write(ctx, websocket.MessageBinary, []byte("BIN"))
			<-ctx.Done()
		},
	})
	gw := &TelnetWsGateway{WSURL: wsURL + "/ws", ColorMode: colors.ModePassthrough}
	ln, _ := gw.Start("127.0.0.1", 0)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go gw.Serve(ctx, ln) //nolint:errcheck

	conn := dialTelnet(t, ln)
	if got := readWithTimeout(t, conn, 3); string(got) != "BIN" {
		t.Fatalf("binary frame delivered as %q, want BIN", got)
	}
}

// TestTelnetGatewayNormalClosure covers the WS-close-status branch of
// pumpWSToClient and the normal-closure return of runGatewaySession: the
// upstream closes with StatusNormalClosure, so the gateway stops (no reconnect)
// and closes the client.
func TestTelnetGatewayNormalClosure(t *testing.T) {
	wsURL := startScriptedWS(t, map[string]func(context.Context, *websocket.Conn){
		"/ws": func(_ context.Context, conn *websocket.Conn) {
			_ = conn.Close(websocket.StatusNormalClosure, "bye")
		},
	})
	gw := &TelnetWsGateway{WSURL: wsURL + "/ws", MaxReconnects: 3, ReconnectDelay: time.Second}
	ln, _ := gw.Start("127.0.0.1", 0)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go gw.Serve(ctx, ln) //nolint:errcheck

	conn := dialTelnet(t, ln)
	expectClosed(t, conn)
}

// TestTelnetGatewayRedirectFollow drives a real same-origin redirect end to
// end: the first upstream hop emits a redirect control frame, and the gateway
// reconnects to the rewritten path (served from the same origin) which echoes.
func TestTelnetGatewayRedirectFollow(t *testing.T) {
	redirectFrame, err := controlchannel.EncodeControlFrame(map[string]any{"type": "redirect", "path": "/ws2"})
	if err != nil {
		t.Fatal(err)
	}
	wsURL := startScriptedWS(t, map[string]func(context.Context, *websocket.Conn){
		"/ws": func(ctx context.Context, conn *websocket.Conn) {
			_ = conn.Write(ctx, websocket.MessageText, []byte(redirectFrame))
			<-ctx.Done()
		},
		"/ws2": func(ctx context.Context, conn *websocket.Conn) {
			dec := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
			for {
				typ, msg, rerr := conn.Read(ctx)
				if rerr != nil {
					return
				}
				if typ != websocket.MessageText {
					continue
				}
				events, _ := dec.Feed(string(msg))
				for _, ev := range events {
					if d, ok := ev.(controlchannel.DataChunk); ok {
						_ = conn.Write(ctx, websocket.MessageText, []byte(controlchannel.EncodeTerminalData(d.Data)))
					}
				}
			}
		},
	})
	gw := &TelnetWsGateway{WSURL: wsURL + "/ws", ColorMode: colors.ModePassthrough, MaxReconnects: 2}
	ln, _ := gw.Start("127.0.0.1", 0)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go gw.Serve(ctx, ln) //nolint:errcheck

	conn := dialTelnet(t, ln)
	// Let the redirect land and the reconnect to /ws2 complete before writing,
	// so the payload is pumped to the echo hop rather than the discarded first
	// hop. Retry the write so a still-in-flight reconnect cannot drop it.
	deadline := time.Now().Add(4 * time.Second)
	for time.Now().Before(deadline) {
		time.Sleep(150 * time.Millisecond)
		if _, err := conn.Write([]byte("redir")); err != nil {
			t.Fatalf("write: %v", err)
		}
		_ = conn.SetReadDeadline(time.Now().Add(400 * time.Millisecond))
		buf := make([]byte, 64)
		n, _ := conn.Read(buf)
		if strings.Contains(string(buf[:n]), "redir") {
			return
		}
	}
	t.Fatal("post-redirect echo never observed")
}

// TestTelnetGatewayReplyDuringPump covers pumpClientToWS writing a negotiation
// reply back to the client: after the initial IAC handshake completes, a fresh
// IAC WILL TTYPE arriving mid-session makes the negotiator emit an SB TTYPE
// SEND reply with no upstream payload.
func TestTelnetGatewayReplyDuringPump(t *testing.T) {
	wsURL, _ := startEchoWS(t)
	gw := &TelnetWsGateway{
		WSURL: wsURL, ColorMode: colors.ModePassthrough,
		IacNegotiate: true, IacNegotiateTimeout: 2 * time.Second,
	}
	ln, _ := gw.Start("127.0.0.1", 0)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go gw.Serve(ctx, ln) //nolint:errcheck

	conn := dialTelnet(t, ln)
	// Fully complete the pre-connect handshake for BOTH requested options so
	// negotiate() returns (Done) and the pump takes over. Answering only TTYPE
	// would leave negotiate() blocked waiting on NEW-ENVIRON, and the later
	// mid-session IAC would be consumed there instead of by the pump.
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	hdr := make([]byte, 64)
	_, _ = conn.Read(hdr)
	handshake := append([]byte{iacIAC, iacWILL, optTTYPE, iacIAC, iacSB, optTTYPE, subIS}, []byte("xterm")...)
	handshake = append(handshake, iacIAC, iacSE)
	handshake = append(handshake, iacIAC, iacWILL, optNewEnviron, iacIAC, iacSB, optNewEnviron, subIS, iacIAC, iacSE)
	_, _ = conn.Write(handshake)

	// Drain everything the negotiate() phase wrote back, so the next reply we
	// read can only come from pumpClientToWS's mid-session reply path.
	_ = conn.SetReadDeadline(time.Now().Add(400 * time.Millisecond))
	drain := make([]byte, 256)
	for {
		if _, err := conn.Read(drain); err != nil {
			break
		}
	}

	// Now, mid-session, ask again: the pump must reply with SB TTYPE SEND.
	_, _ = conn.Write([]byte{iacIAC, iacWILL, optTTYPE})
	_ = conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	buf := make([]byte, 0, 64)
	tmp := make([]byte, 64)
	want := []byte{iacIAC, iacSB, optTTYPE, subSEND, iacIAC, iacSE}
	for i := 0; i < 8; i++ {
		n, err := conn.Read(tmp)
		buf = append(buf, tmp[:n]...)
		if strings.Contains(string(buf), string(want)) {
			return
		}
		if err != nil {
			break
		}
	}
	t.Fatalf("did not observe SB TTYPE SEND reply, got %v", buf)
}

// TestTelnetServeAcceptError covers the non-context Accept-error return: closing
// the listener out from under Serve (without cancelling ctx) surfaces the error.
func TestTelnetServeAcceptError(t *testing.T) {
	gw := &TelnetWsGateway{WSURL: "ws://x/ws"}
	ln, err := gw.Start("127.0.0.1", 0)
	if err != nil {
		t.Fatalf("start: %v", err)
	}
	errCh := make(chan error, 1)
	go func() { errCh <- gw.Serve(context.Background(), ln) }()
	time.Sleep(50 * time.Millisecond)
	_ = ln.Close()
	select {
	case err := <-errCh:
		if err == nil {
			t.Fatal("Serve should return the accept error when ctx is live")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Serve did not return after listener close")
	}
}

// TestSSHServeAcceptError is the SSH counterpart of TestTelnetServeAcceptError.
func TestSSHServeAcceptError(t *testing.T) {
	gw := &SshWsGateway{WSURL: "ws://x/ws"}
	ln, err := gw.Start("127.0.0.1", 0)
	if err != nil {
		t.Fatalf("start: %v", err)
	}
	errCh := make(chan error, 1)
	go func() { errCh <- gw.Serve(context.Background(), ln) }()
	time.Sleep(50 * time.Millisecond)
	_ = ln.Close()
	select {
	case err := <-errCh:
		if err == nil {
			t.Fatal("Serve should return the accept error when ctx is live")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Serve did not return after listener close")
	}
}

// TestSSHGatewayHandshakeFailure covers ssh handleConn's NewServerConn error
// branch: a raw client that speaks garbage instead of the SSH handshake is
// dropped and its connection closed.
func TestSSHGatewayHandshakeFailure(t *testing.T) {
	wsURL, _ := startEchoWS(t)
	gw := &SshWsGateway{WSURL: wsURL}
	ln := startSSHGateway(t, gw)

	conn, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close() //nolint:errcheck
	// Send a bogus banner; the SSH server handshake will reject it.
	_, _ = conn.Write([]byte("NOT-SSH garbage\r\n"))
	expectClosed(t, conn)
}

// TestTelnetNegotiateTimeout covers the negotiate() deadline branch: with a
// sub-tick timeout the negotiation window is already expired on the first loop
// check, so it breaks without a reply and the session still pumps.
func TestTelnetNegotiateTimeout(t *testing.T) {
	wsURL, _ := startEchoWS(t)
	gw := &TelnetWsGateway{
		WSURL: wsURL, ColorMode: colors.ModePassthrough,
		IacNegotiate: true, IacNegotiateTimeout: 1, // 1ns → expired immediately
	}
	ln, _ := gw.Start("127.0.0.1", 0)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go gw.Serve(ctx, ln) //nolint:errcheck

	conn := dialTelnet(t, ln)
	if _, err := conn.Write([]byte("hi")); err != nil {
		t.Fatalf("write: %v", err)
	}
	scanFor(t, conn, "hi", 4*time.Second)
}

// TestTelnetColormodeAppendAmp covers handleConn's "&" separator branch: when
// the base WS URL already carries a query, the derived colormode is appended
// with "&" rather than "?".
func TestTelnetColormodeAppendAmp(t *testing.T) {
	wsURL, rec := startEchoWS(t)
	gw := &TelnetWsGateway{
		WSURL: wsURL + "?x=1", ColorMode: colors.ModePassthrough,
		IacNegotiate: true, IacNegotiateTimeout: 2 * time.Second,
	}
	ln, _ := gw.Start("127.0.0.1", 0)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go gw.Serve(ctx, ln) //nolint:errcheck

	conn := dialTelnet(t, ln)
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	hdr := make([]byte, 64)
	_, _ = conn.Read(hdr)
	ttype := append([]byte{iacIAC, iacWILL, optTTYPE, iacIAC, iacSB, optTTYPE, subIS}, []byte("xterm-256color")...)
	ttype = append(ttype, iacIAC, iacSE)
	_, _ = conn.Write(ttype)
	_ = conn.SetReadDeadline(time.Time{})

	select {
	case q := <-rec.queries:
		if !strings.Contains(q, "x=1&colormode=256") {
			t.Fatalf("upstream query = %q, want to contain x=1&colormode=256", q)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no upstream connection observed")
	}
}

// TestTelnetClientDisconnect covers pumpClientToWS's clientDone branch: when the
// telnet client closes while the upstream is still open, the pump ends via the
// client-done signal (not context cancellation).
func TestTelnetClientDisconnect(t *testing.T) {
	wsURL, _ := startEchoWS(t)
	gw := &TelnetWsGateway{WSURL: wsURL, ColorMode: colors.ModePassthrough}
	ln, _ := gw.Start("127.0.0.1", 0)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go gw.Serve(ctx, ln) //nolint:errcheck

	conn, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	// Let the pump reach its idle select, then disconnect the client.
	time.Sleep(250 * time.Millisecond)
	_ = conn.Close()
	// Give the reader goroutine time to observe EOF and close clientDone.
	time.Sleep(250 * time.Millisecond)
}

// TestTelnetReconnectAndResume covers the telnet showReconnect banner and the
// resume-token replay: the first upstream hands out a session token then drops
// abnormally; the gateway reconnects, replays a resume frame, and echoes.
func TestTelnetReconnectAndResume(t *testing.T) {
	tokenFrame, err := controlchannel.EncodeControlFrame(map[string]any{"type": "session_token", "token": "tok-1"})
	if err != nil {
		t.Fatal(err)
	}
	var count int32
	gotResume := make(chan struct{}, 1)
	wsURL := startScriptedWS(t, map[string]func(context.Context, *websocket.Conn){
		"/ws": func(ctx context.Context, conn *websocket.Conn) {
			if atomic.AddInt32(&count, 1) == 1 {
				_ = conn.Write(ctx, websocket.MessageText, []byte(tokenFrame))
				time.Sleep(50 * time.Millisecond)
				_ = conn.CloseNow() // abnormal drop → gateway reconnects
				return
			}
			ctrl := make(chan string, 8)
			go func() {
				for ct := range ctrl {
					if ct == "resume" {
						select {
						case gotResume <- struct{}{}:
						default:
						}
					}
				}
			}()
			echoWSChunks(ctx, conn, ctrl)
		},
	})
	gw := &TelnetWsGateway{
		WSURL: wsURL + "/ws", ColorMode: colors.ModePassthrough,
		MaxReconnects: 3, ReconnectDelay: 20 * time.Millisecond,
	}
	ln, _ := gw.Start("127.0.0.1", 0)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go gw.Serve(ctx, ln) //nolint:errcheck

	conn := dialTelnet(t, ln)
	// The banner is written to the client when the reconnect is scheduled.
	scanFor(t, conn, "reconnecting", 4*time.Second)
	select {
	case <-gotResume:
	case <-time.After(4 * time.Second):
		t.Fatal("resume frame was not replayed upstream after reconnect")
	}
}

// TestTelnetTLSUpstream covers the tls.Config dial branch of pumpOnce: the
// upstream is a wss:// server reached with a custom TLS config.
func TestTelnetTLSUpstream(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		conn.SetReadLimit(-1)
		echoWSChunks(r.Context(), conn, nil)
	}))
	t.Cleanup(srv.Close)

	pool := x509.NewCertPool()
	pool.AddCert(srv.Certificate())
	wsURL := "wss://" + strings.TrimPrefix(srv.URL, "https://") + "/ws"
	gw := &TelnetWsGateway{
		WSURL: wsURL, ColorMode: colors.ModePassthrough,
		TLSConfig: &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12},
	}
	ln, _ := gw.Start("127.0.0.1", 0)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go gw.Serve(ctx, ln) //nolint:errcheck

	conn := dialTelnet(t, ln)
	if _, err := conn.Write([]byte("tls")); err != nil {
		t.Fatalf("write: %v", err)
	}
	scanFor(t, conn, "tls", 4*time.Second)
}

// TestSSHReconnectBanner covers handleSession's showReconnect closure: the SSH
// session's upstream drops once, so the reconnect banner is written to the SSH
// channel before the gateway reconnects and resumes echoing.
func TestSSHReconnectBanner(t *testing.T) {
	var count int32
	wsURL := startScriptedWS(t, map[string]func(context.Context, *websocket.Conn){
		"/ws": func(ctx context.Context, conn *websocket.Conn) {
			if atomic.AddInt32(&count, 1) == 1 {
				time.Sleep(50 * time.Millisecond)
				_ = conn.CloseNow()
				return
			}
			echoWSChunks(ctx, conn, nil)
		},
	})
	gw := &SshWsGateway{
		WSURL: wsURL + "/ws", ColorMode: colors.ModePassthrough,
		MaxReconnects: 3, ReconnectDelay: 20 * time.Millisecond,
	}
	ln := startSSHGateway(t, gw)
	client := dialSSHClient(t, ln.Addr().String())
	sess, err := client.NewSession()
	if err != nil {
		t.Fatalf("new session: %v", err)
	}
	defer sess.Close() //nolint:errcheck
	_ = sess.RequestPty("xterm", 24, 80, ssh.TerminalModes{})
	// Hold the stdin pipe open so the client does not send a channel EOF, which
	// would end the session before the upstream drop can trigger a reconnect.
	stdin, _ := sess.StdinPipe()
	defer stdin.Close() //nolint:errcheck
	stdout, _ := sess.StdoutPipe()
	if err := sess.Shell(); err != nil {
		t.Fatalf("shell: %v", err)
	}
	scanFor(t, stdout, "reconnecting", 5*time.Second)
}
