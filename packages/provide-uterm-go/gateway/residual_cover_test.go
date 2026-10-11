//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package gateway

import (
	"context"
	"crypto/ed25519"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/coder/websocket"
	"golang.org/x/crypto/ssh"
)

// recordingWSServer accepts WebSocket upgrades and forwards every text message
// it receives to got (until the client goes away).
func recordingWSServer(t *testing.T) (string, <-chan string) {
	t.Helper()
	got := make(chan string, 16)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer c.CloseNow() //nolint:errcheck // test cleanup
		for {
			_, msg, err := c.Read(r.Context())
			if err != nil {
				close(got)
				return
			}
			got <- string(msg)
		}
	}))
	t.Cleanup(srv.Close)
	return "ws" + strings.TrimPrefix(srv.URL, "http"), got
}

func basePumpConfig(st *controlState) pumpConfig {
	return pumpConfig{
		clientRx:       make(chan []byte),
		clientDone:     make(chan struct{}),
		writeClient:    func([]byte) error { return nil },
		readTransform:  func(b []byte) ([]byte, []byte) { return b, nil },
		writeTransform: func(b []byte) []byte { return b },
		st:             st,
	}
}

func TestPumpOnceIdentityFrameEncodeError(t *testing.T) {
	url, got := recordingWSServer(t)
	cfg := basePumpConfig(&controlState{})
	cfg.identityFrame = map[string]any{"bad": make(chan int)}
	code, err := pumpOnce(context.Background(), url, cfg)
	if err == nil || code != -1 {
		t.Fatalf("pumpOnce = %d, %v; want -1 and encode error", code, err)
	}
	// The pump gave up before sending anything: the server sees only the close.
	if msg, ok := <-got; ok {
		t.Fatalf("server received %q before the failed identity frame", msg)
	}
}

// failPumpFrame makes sendPumpFrame fail on the first frame of type failType,
// recording the type of every frame offered.
func failPumpFrame(t *testing.T, failType string) (*[]string, error) {
	t.Helper()
	sentinel := errors.New(failType + " write failed")
	var offered []string
	orig := sendPumpFrame
	sendPumpFrame = func(ctx context.Context, conn *websocket.Conn, f map[string]any) error {
		typ, _ := f["type"].(string)
		offered = append(offered, typ)
		if typ == failType {
			return sentinel
		}
		return orig(ctx, conn, f)
	}
	t.Cleanup(func() { sendPumpFrame = orig })
	return &offered, sentinel
}

func TestPumpOnceOpeningFrameSendErrors(t *testing.T) {
	cases := []struct {
		failType string
		token    *tokenRec
		want     []string
	}{
		{"resume", &tokenRec{token: "tok-1"}, []string{"resume"}},
		{"hello", nil, []string{"hello"}},
	}
	for _, tc := range cases {
		t.Run(tc.failType, func(t *testing.T) {
			url, got := recordingWSServer(t)
			offered, sentinel := failPumpFrame(t, tc.failType)
			code, err := pumpOnce(context.Background(), url, basePumpConfig(&controlState{token: tc.token}))
			if !errors.Is(err, sentinel) || code != -1 {
				t.Fatalf("pumpOnce = %d, %v; want -1 and the %s failure", code, err, tc.failType)
			}
			if strings.Join(*offered, ",") != strings.Join(tc.want, ",") {
				t.Fatalf("frames offered = %v, want %v", *offered, tc.want)
			}
			if msg, ok := <-got; ok {
				t.Fatalf("server received %q; nothing may follow a failed opening frame", msg)
			}
		})
	}
}

func TestPumpClientToWSStopsOnWriteError(t *testing.T) {
	url, _ := recordingWSServer(t)
	conn, resp, err := websocket.Dial(context.Background(), url, nil)
	if resp != nil && resp.Body != nil {
		_ = resp.Body.Close()
	}
	if err != nil {
		t.Fatal(err)
	}
	_ = conn.CloseNow() // every later Write fails with net.ErrClosed

	rx := make(chan []byte, 1)
	rx <- []byte("typed")
	var replies [][]byte
	cfg := basePumpConfig(&controlState{})
	cfg.clientRx = rx
	cfg.readTransform = func(b []byte) ([]byte, []byte) { return b, []byte("echo") }
	cfg.writeClient = func(b []byte) error { replies = append(replies, b); return nil }

	// Returns (instead of blocking for the next chunk) because the write failed.
	pumpClientToWS(context.Background(), conn, cfg)
	if len(rx) != 0 || len(replies) != 1 || string(replies[0]) != "echo" {
		t.Fatalf("rx left=%d replies=%q", len(rx), replies)
	}
}

// chunkReader yields one chunk, then reports EOF.
type chunkReader struct{ sent bool }

func (r *chunkReader) Read(p []byte) (int, error) {
	if r.sent {
		return 0, io.EOF
	}
	r.sent = true
	return copy(p, "hi"), nil
}

func TestReadClientAbandonsChunkOnCancel(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	r := &chunkReader{}
	rx := make(chan []byte) // nobody receives
	done := make(chan struct{})
	readClient(ctx, r, rx, done)
	select {
	case <-done:
	default:
		t.Fatal("done not closed")
	}
	if !r.sent {
		t.Fatal("client was never read")
	}
}

func TestReadClientForwardsUntilEOF(t *testing.T) {
	rx := make(chan []byte, 1)
	done := make(chan struct{})
	readClient(context.Background(), &chunkReader{}, rx, done)
	if got := string(<-rx); got != "hi" {
		t.Fatalf("forwarded %q", got)
	}
	<-done
}

func TestHostSignerReadErrorOnSocket(t *testing.T) {
	// A unix socket stats as a non-directory but cannot be opened for reading,
	// even as root.
	// os.MkdirTemp("", ...) keeps the path under the sun_path length limit,
	// which t.TempDir() can exceed on macOS.
	dir, err := os.MkdirTemp("", "gwk")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	path := filepath.Join(dir, "k.sock")
	ln, err := net.Listen("unix", path)
	if err != nil {
		t.Skipf("unix sockets unavailable: %v", err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	gw := &SshWsGateway{ServerKey: path}
	if s, err := gw.hostSigner(); err == nil || s != nil {
		t.Fatalf("hostSigner = %v, %v; want read error", s, err)
	}
}

func TestHostSignerEphemeralKeyError(t *testing.T) {
	sentinel := errors.New("entropy unavailable")
	orig := generateHostKey
	generateHostKey = func() (ed25519.PrivateKey, error) { return nil, sentinel }
	t.Cleanup(func() { generateHostKey = orig })
	gw := &SshWsGateway{}
	if s, err := gw.hostSigner(); !errors.Is(err, sentinel) || s != nil {
		t.Fatalf("hostSigner = %v, %v; want sentinel", s, err)
	}
	if _, err := gw.serverConfig(); !errors.Is(err, sentinel) {
		t.Fatalf("serverConfig err = %v; want sentinel", err)
	}
}

// rejectingNewChannel is an ssh.NewChannel whose Accept fails, as it does when
// the underlying connection drops between the channel open and its accept.
type rejectingNewChannel struct{ accepts int }

func (c *rejectingNewChannel) Accept() (ssh.Channel, <-chan *ssh.Request, error) {
	c.accepts++
	return nil, nil, io.ErrClosedPipe
}
func (*rejectingNewChannel) Reject(ssh.RejectionReason, string) error { return nil }
func (*rejectingNewChannel) ChannelType() string                      { return "session" }
func (*rejectingNewChannel) ExtraData() []byte                        { return nil }

func TestHandleSessionAcceptError(t *testing.T) {
	gw := &SshWsGateway{WSURL: "ws://127.0.0.1:1/ws"}
	nc := &rejectingNewChannel{}
	// Returns immediately without dialing upstream or touching a nil channel.
	gw.handleSession(context.Background(), nc, nil)
	if nc.accepts != 1 {
		t.Fatalf("Accept called %d times", nc.accepts)
	}
}
