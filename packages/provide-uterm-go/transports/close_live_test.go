//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

// Typed closes observed over real loopback sockets (WebSocket, SSH).

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/coder/websocket"
	"golang.org/x/crypto/ssh"
)

// --- WebSocket ---------------------------------------------------------------

// wsClosingServer accepts, then closes with code/reason. With frame false it
// drops the connection without sending a close frame.
func wsClosingServer(t *testing.T, code websocket.StatusCode, reason string, frame bool) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		if frame {
			_ = c.Close(code, reason)
			return
		}
		_ = c.CloseNow()
	}))
	t.Cleanup(srv.Close)
	return srv
}

func connectWS(t *testing.T, srv *httptest.Server) *WebSocketTransport {
	t.Helper()
	tr := NewWebSocketTransport()
	if err := tr.Connect(context.Background(), "", 0, ConnectOptions{WS: WSOptions{URL: wsURL(srv.URL)}}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	return tr
}

func TestWebSocketReceiveReportsTheServersClose(t *testing.T) {
	tr := connectWS(t, wsClosingServer(t, websocket.StatusGoingAway, "going away", true))
	_, err := tr.Receive(context.Background(), 4096, 2*time.Second)
	got := requireClose(t, err, "connection closed", CloseRemote)
	if got.Code == nil || *got.Code != 1001 || got.Reason != "going away" || got.Detail == "" {
		t.Errorf("close = %+v (%v)", got, err)
	}
	if tr.IsConnected() {
		t.Error("should be disconnected")
	}
}

func TestWebSocketReceiveWithoutCloseFrameIsUnknown(t *testing.T) {
	tr := connectWS(t, wsClosingServer(t, 0, "", false))
	_, err := tr.Receive(context.Background(), 4096, 2*time.Second)
	got := requireClose(t, err, "connection closed", CloseUnknown)
	if got.Code != nil || got.Detail == "" {
		t.Errorf("close = %+v", got)
	}
}

func TestWebSocketSendAfterTheServerClosedReportsItsClose(t *testing.T) {
	tr := connectWS(t, wsClosingServer(t, websocket.StatusGoingAway, "going away", true))
	tr.mu.Lock()
	closed := tr.closed
	tr.mu.Unlock()
	select {
	case <-closed: // the reader has seen the close; Receive was never called
	case <-time.After(2 * time.Second):
		t.Fatal("reader never saw the server close")
	}
	err := tr.Send(context.Background(), []byte("x"))
	got := requireClose(t, err, "connection closed", CloseRemote)
	if got.Code == nil || *got.Code != 1001 || got.Reason != "going away" {
		t.Errorf("close = %+v", got)
	}
}

func TestWebSocketSendFailureWhileTheReaderIsAliveIsUnknown(t *testing.T) {
	tr := connectWS(t, wsEchoServer(t, nil))
	// Stand in a reader that has not finished, then break the socket under
	// it: the write fails before any close has been recorded.
	tr.mu.Lock()
	conn := tr.conn
	tr.closed = make(chan struct{})
	tr.mu.Unlock()
	_ = conn.CloseNow()

	err := tr.Send(context.Background(), []byte("x"))
	got := requireClose(t, err, "connection closed", CloseUnknown)
	if got.Code != nil || got.Detail == "" {
		t.Errorf("close = %+v", got)
	}
	if !errors.Is(err, net.ErrClosed) {
		t.Errorf("the write error must stay reachable: %v", err)
	}
}

func TestWebSocketDisconnectIsALocalClose(t *testing.T) {
	tr := connectWS(t, wsEchoServer(t, nil))
	tr.mu.Lock()
	closed, end := tr.closed, tr.end
	tr.mu.Unlock()
	_ = tr.Disconnect(context.Background())
	<-closed
	if got := end.close; got.Initiator != CloseLocal || got.Code == nil || *got.Code != 1000 {
		t.Errorf("close = %+v", got)
	}
	// A second Disconnect keeps the first close frame.
	_ = tr.Disconnect(context.Background())
}

func TestWebSocketDisconnectWhileAMessageIsUndeliveredIsALocalClose(t *testing.T) {
	tr := connectWS(t, wsEchoServer(t, nil))
	ctx := context.Background()
	if err := tr.Send(ctx, []byte("echo me")); err != nil {
		t.Fatalf("send: %v", err)
	}
	// Nobody calls Receive, so the reader parks on delivering the echo.
	time.Sleep(200 * time.Millisecond)
	tr.mu.Lock()
	closed, end := tr.closed, tr.end
	tr.mu.Unlock()
	_ = tr.Disconnect(ctx)
	<-closed
	if got := end.close; got.Initiator != CloseLocal || got.Code == nil || *got.Code != 1000 {
		t.Errorf("close = %+v", got)
	}
}

// --- SSH ---------------------------------------------------------------------

// startSSHServerExit replies to shell, sends the given exit request (if any),
// then closes the session channel.
func startSSHServerExit(t *testing.T, reqType string, payload []byte) (string, int) {
	t.Helper()
	_, priv, _ := ed25519.GenerateKey(rand.Reader)
	signer, _ := ssh.NewSignerFromKey(priv)
	cfg := &ssh.ServerConfig{NoClientAuth: true}
	cfg.AddHostKey(signer)
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		for {
			nConn, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				_, chans, reqs, err := ssh.NewServerConn(c, cfg)
				if err != nil {
					return
				}
				go ssh.DiscardRequests(reqs)
				for newCh := range chans {
					ch, requests, err := newCh.Accept()
					if err != nil {
						continue
					}
					go func() {
						for req := range requests {
							_ = req.Reply(true, nil)
							if req.Type == "shell" {
								if reqType != "" {
									_, _ = ch.SendRequest(reqType, false, payload)
								}
								_ = ch.Close()
							}
						}
					}()
				}
			}(nConn)
		}
	}()
	return "127.0.0.1", ln.Addr().(*net.TCPAddr).Port
}

func sshCloseAfterShell(t *testing.T, reqType string, payload []byte) TransportClose {
	t.Helper()
	host, port := startSSHServerExit(t, reqType, payload)
	tr := NewSSHTransport()
	ctx := context.Background()
	opts := ConnectOptions{SSH: SSHOptions{User: "u", Password: "pw", InsecureSkipHostKeyVerify: true}}
	if err := tr.Connect(ctx, host, port, opts); err != nil {
		t.Fatalf("connect: %v", err)
	}
	for i := 0; i < 20; i++ {
		if _, err := tr.Receive(ctx, 4096, 200*time.Millisecond); err != nil {
			return requireClose(t, err, "connection closed", CloseRemote)
		}
	}
	t.Fatal("the channel close never surfaced")
	return TransportClose{}
}

func TestSSHExitStatusIsTheCloseReason(t *testing.T) {
	got := sshCloseAfterShell(t, "exit-status", ssh.Marshal(struct{ Status uint32 }{3}))
	if got.Summary() != "remote close exit status 3" {
		t.Errorf("summary = %q", got.Summary())
	}
	got = sshCloseAfterShell(t, "exit-status", ssh.Marshal(struct{ Status uint32 }{0}))
	if got.Summary() != "remote close exit status 0" {
		t.Errorf("summary = %q", got.Summary())
	}
}

func TestSSHExitSignalIsTheCloseReason(t *testing.T) {
	payload := ssh.Marshal(struct {
		Signal     string
		CoreDumped bool
		Error      string
		Lang       string
	}{"TERM", false, "", ""})
	got := sshCloseAfterShell(t, "exit-signal", payload)
	if got.Summary() != "remote close signal TERM" {
		t.Errorf("summary = %q", got.Summary())
	}
}

type waiterFunc func() error

func (f waiterFunc) Wait() error { return f() }

func TestSSHExitReason(t *testing.T) {
	if got := sshExitReason(nil); got != "exit status 0" {
		t.Errorf("nil = %q", got)
	}
	if got := sshExitReason(&ssh.ExitMissingError{}); got != "" {
		t.Errorf("missing = %q", got)
	}
	if got := sshExitReason(errors.New("other")); got != "" {
		t.Errorf("other = %q", got)
	}
}

func TestWaitExitReasonGivesUpAfterTheLimit(t *testing.T) {
	block := make(chan struct{})
	defer close(block)
	w := waiterFunc(func() error { <-block; return nil })
	start := time.Now()
	if got := waitExitReason(w, 20*time.Millisecond); got != "" {
		t.Errorf("reason = %q", got)
	}
	if time.Since(start) > time.Second {
		t.Error("waitExitReason did not honour its limit")
	}
}

func TestCloseFromSSHRead(t *testing.T) {
	open := make(chan struct{})
	quit := make(chan struct{})
	close(quit)
	w := waiterFunc(func() error { return nil })

	if got := closeFromSSHRead(errors.New("x"), quit, w); got != (TransportClose{Initiator: CloseLocal}) {
		t.Errorf("after Disconnect = %+v", got)
	}
	if got := closeFromSSHRead(io.EOF, open, w); got.Summary() != "remote close exit status 0" {
		t.Errorf("EOF = %q", got.Summary())
	}
	got := closeFromSSHRead(errors.New("protocol error"), open, w)
	if got.Summary() != "unknown close (*errors.errorString: protocol error)" {
		t.Errorf("other = %q", got.Summary())
	}
}

func TestSSHSendAfterThePeerClosed(t *testing.T) {
	host, port := startSSHServerExit(t, "exit-status", ssh.Marshal(struct{ Status uint32 }{1}))
	tr := NewSSHTransport()
	ctx := context.Background()
	opts := ConnectOptions{SSH: SSHOptions{User: "u", Password: "pw", InsecureSkipHostKeyVerify: true}}
	if err := tr.Connect(ctx, host, port, opts); err != nil {
		t.Fatalf("connect: %v", err)
	}
	tr.mu.Lock()
	closed := tr.closed
	tr.mu.Unlock()
	select {
	case <-closed:
	case <-time.After(3 * time.Second):
		t.Fatal("reader never saw the channel close")
	}
	err := tr.Send(ctx, []byte("x"))
	got := requireClose(t, err, "send failed", CloseRemote)
	if got.Reason != "exit status 1" {
		t.Errorf("close = %+v", got)
	}
}

// failingWriter is an io.WriteCloser whose Write fails with err.
type failingWriter struct{ err error }

func (w failingWriter) Write([]byte) (int, error) { return 0, w.err }
func (w failingWriter) Close() error              { return nil }

func TestSSHSendFailureWhileTheReaderIsAlive(t *testing.T) {
	for _, tc := range []struct {
		err       error
		initiator CloseInitiator
	}{{io.EOF, CloseRemote}, {errors.New("boom"), CloseUnknown}} {
		tr := NewSSHTransport()
		tr.stdin = failingWriter{tc.err}
		tr.closed = make(chan struct{})
		tr.end = &TransportClose{}
		err := tr.Send(context.Background(), []byte("x"))
		requireClose(t, err, "send failed", tc.initiator)
	}
}
