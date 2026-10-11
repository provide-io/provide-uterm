//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package tunnelclient

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/creack/pty"
	"golang.org/x/term"
)

// zeroBackoff removes the real backoff waits for the duration of a test.
func zeroBackoff(t *testing.T) {
	t.Helper()
	orig := backoffSchedule
	backoffSchedule = [len(backoffSchedule)]time.Duration{}
	t.Cleanup(func() { backoffSchedule = orig })
}

// flakyServer rejects the first `rejects` upgrade attempts with 403, then
// accepts and holds the connection until the client closes it.
func flakyServer(t *testing.T, rejects int64) (string, *atomic.Int64) {
	t.Helper()
	var attempts atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if attempts.Add(1) <= rejects {
			http.Error(w, "nope", http.StatusForbidden)
			return
		}
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer c.CloseNow() //nolint:errcheck // test cleanup
		_, _, _ = c.Read(r.Context())
	}))
	t.Cleanup(srv.Close)
	return "ws" + strings.TrimPrefix(srv.URL, "http"), &attempts
}

func TestConnectRejectedHandshakeReturnsError(t *testing.T) {
	url, attempts := flakyServer(t, 1)
	c := NewClient(url, "tok")
	err := c.Connect(context.Background())
	if err == nil || !strings.Contains(err.Error(), "403") {
		t.Fatalf("Connect err = %v; want 403 handshake failure", err)
	}
	if c.Connected() || attempts.Load() != 1 {
		t.Fatalf("connected=%v attempts=%d", c.Connected(), attempts.Load())
	}
}

func TestReconnectLoopUnlimitedPastScheduleEnd(t *testing.T) {
	zeroBackoff(t)
	// More rejections than schedule entries, so the index clamps to the last
	// delay; maxAttempts 0 means "keep going" until a connect succeeds.
	rejects := int64(len(backoffSchedule) + 2)
	url, attempts := flakyServer(t, rejects)
	c := NewClient(url, "")
	if err := c.ReconnectLoop(context.Background(), 0); err != nil {
		t.Fatalf("ReconnectLoop: %v", err)
	}
	defer func() { _ = c.Close() }()
	if !c.Connected() || attempts.Load() != rejects+1 {
		t.Fatalf("connected=%v attempts=%d want %d", c.Connected(), attempts.Load(), rejects+1)
	}
}

func TestReconnectLoopExhaustsAttempts(t *testing.T) {
	zeroBackoff(t)
	url, attempts := flakyServer(t, 100)
	c := NewClient(url, "")
	if err := c.ReconnectLoop(context.Background(), 2); !errors.Is(err, ErrNotConnected) {
		t.Fatalf("ReconnectLoop err = %v; want ErrNotConnected", err)
	}
	if attempts.Load() != 2 || c.Connected() {
		t.Fatalf("attempts=%d connected=%v", attempts.Load(), c.Connected())
	}
}

func TestRecvReadErrorAfterServerClose(t *testing.T) {
	url := wsServer(t, func(ctx context.Context, c *websocket.Conn, _ string) {
		_ = c.Close(websocket.StatusNormalClosure, "bye")
	})
	c := NewClient(url, "")
	if err := c.Connect(context.Background()); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Close() }()
	f, err := c.Recv(context.Background())
	if websocket.CloseStatus(err) != websocket.StatusNormalClosure {
		t.Fatalf("Recv = %+v, %v; want normal-closure error", f, err)
	}
}

func TestMustMarshalPanicsOnUnencodable(t *testing.T) {
	defer func() {
		if r := recover(); r == nil {
			t.Fatal("mustMarshal did not panic on a channel value")
		}
	}()
	mustMarshal(make(chan int))
}

func TestSpawnPTYFallsBackToBinSh(t *testing.T) {
	t.Setenv("SHELL", "")
	s, err := SpawnPTY(nil)
	if err != nil {
		t.Fatalf("SpawnPTY: %v", err)
	}
	defer func() { _ = s.Close() }()
	if got := s.cmd.Path; got != "/bin/sh" {
		t.Fatalf("spawned %q, want /bin/sh", got)
	}
}

func TestSpawnedPtyTermSizeFallbackOnNonTTY(t *testing.T) {
	f, err := os.Create(filepath.Join(t.TempDir(), "notatty"))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = f.Close() }()
	s := &SpawnedPty{master: f}
	if cols, rows := s.TermSize(); cols != 80 || rows != 24 {
		t.Fatalf("TermSize = %dx%d, want 80x24", cols, rows)
	}
}

func TestTtyProxyStartMakeRawError(t *testing.T) {
	ptmx, tty, err := pty.Open()
	if err != nil {
		t.Skipf("cannot open pty pair: %v", err)
	}
	defer func() { _ = ptmx.Close(); _ = tty.Close() }()
	sentinel := errors.New("tcsetattr failed")
	orig := makeRaw
	makeRaw = func(int) (*term.State, error) { return nil, sentinel }
	t.Cleanup(func() { makeRaw = orig })
	p := &TtyProxy{in: tty, out: tty}
	cols, rows, err := p.Start()
	if !errors.Is(err, sentinel) || cols != 0 || rows != 0 || p.Active() {
		t.Fatalf("Start = %d,%d,%v active=%v", cols, rows, err, p.Active())
	}
}

func TestTtyProxyCloseActiveWithoutSavedState(t *testing.T) {
	p := &TtyProxy{active: true}
	if err := p.Close(); err != nil || p.Active() {
		t.Fatalf("Close = %v active=%v", err, p.Active())
	}
}
