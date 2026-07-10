//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"sync"
	"time"

	"github.com/creack/pty"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/transports"
)

// PTYTransport spawns a local command in a freshly-allocated pseudo-terminal and
// exposes it as a transports.ConnectionTransport, so the shell connector can
// reuse the same TransportSession machinery as the network connectors. It is
// backed by github.com/creack/pty (the openpty/fork/setsid/TIOCSCTTY dance,
// portable across macOS and Linux) and follows the channel-based Receive design
// of SSHTransport so a parked reader never leaks on Disconnect.
//
// Deviation from Python: there is no PTY connector in the Python server (its
// ShellSessionConnector is an in-memory reference). This transport is the Go
// mechanism that makes the shell connector a real local terminal.
type PTYTransport struct {
	command []string

	mu      sync.Mutex
	master  *os.File
	cmd     *exec.Cmd
	rxCh    chan []byte
	closed  chan struct{} // closed by readLoop when the PTY ends
	quit    chan struct{} // closed by Disconnect to unblock a parked readLoop
	remnant []byte
}

var _ transports.ConnectionTransport = (*PTYTransport)(nil)

// NewPTYTransport returns an unconnected PTY transport that will spawn command
// on Connect. An empty command defaults to $SHELL, then /bin/sh.
func NewPTYTransport(command []string) *PTYTransport {
	return &PTYTransport{command: append([]string(nil), command...)}
}

// resolveCommand returns the argv to spawn, applying the $SHELL/​/bin/sh default.
func (t *PTYTransport) resolveCommand() []string {
	if len(t.command) > 0 {
		return t.command
	}
	shell := os.Getenv("SHELL")
	if shell == "" {
		shell = "/bin/sh"
	}
	return []string{shell}
}

// Connect spawns the command in a new PTY and starts the reader. host/port are
// ignored (there is no network peer); opts.Cols/Rows size the PTY.
func (t *PTYTransport) Connect(_ context.Context, _ string, _ int, opts transports.ConnectOptions) error {
	cols, rows := opts.Cols, opts.Rows
	if cols <= 0 {
		cols = transports.DefaultCols
	}
	if rows <= 0 {
		rows = transports.DefaultRows
	}
	argv := t.resolveCommand()
	c := exec.Command(argv[0], argv[1:]...) //nolint:gosec // operator-chosen command, by design
	master, err := pty.Start(c)
	if err != nil {
		return fmt.Errorf("pty start: %w", err)
	}
	_ = pty.Setsize(master, &pty.Winsize{Rows: uint16(rows), Cols: uint16(cols)}) //nolint:gosec // sizes fit uint16

	t.mu.Lock()
	t.master = master
	t.cmd = c
	t.rxCh = make(chan []byte)
	t.closed = make(chan struct{})
	t.quit = make(chan struct{})
	t.remnant = nil
	rxCh, closed, quit := t.rxCh, t.closed, t.quit
	t.mu.Unlock()

	go t.readLoop(master, rxCh, closed, quit)
	return nil
}

// readLoop pumps PTY master output into rxCh until EOF/error or Disconnect.
func (t *PTYTransport) readLoop(master io.Reader, rxCh chan []byte, closed, quit chan struct{}) {
	defer close(closed)
	buf := make([]byte, 32*1024)
	for {
		n, err := master.Read(buf)
		if n > 0 {
			chunk := make([]byte, n)
			copy(chunk, buf[:n])
			select {
			case rxCh <- chunk:
			case <-quit:
				return
			}
		}
		if err != nil {
			return
		}
	}
}

// Disconnect closes the PTY master and reaps the child. Idempotent.
func (t *PTYTransport) Disconnect(_ context.Context) error {
	t.mu.Lock()
	master := t.master
	cmd := t.cmd
	quit := t.quit
	t.master = nil
	t.cmd = nil
	t.quit = nil
	t.mu.Unlock()

	if quit != nil {
		close(quit)
	}
	if master != nil {
		_ = master.Close()
	}
	if cmd != nil && cmd.Process != nil {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	}
	return nil
}

// Send writes data to the PTY master (delivered to the child's stdin).
func (t *PTYTransport) Send(ctx context.Context, data []byte) error {
	t.mu.Lock()
	master := t.master
	t.mu.Unlock()
	if master == nil {
		return fmt.Errorf("%w: pty send", transports.ErrNotConnected)
	}
	if _, err := master.Write(data); err != nil {
		_ = t.Disconnect(ctx)
		return fmt.Errorf("pty send failed: %w", err)
	}
	return nil
}

// Receive returns up to maxBytes from the PTY, an empty slice on timeout, or
// ErrConnectionClosed when the child exits and its output drains.
func (t *PTYTransport) Receive(ctx context.Context, maxBytes int, timeout time.Duration) ([]byte, error) {
	t.mu.Lock()
	rxCh := t.rxCh
	closed := t.closed
	if rxCh == nil {
		t.mu.Unlock()
		return nil, fmt.Errorf("%w: pty receive", transports.ErrNotConnected)
	}
	if len(t.remnant) > 0 {
		out := t.takeRemnantLocked(maxBytes)
		t.mu.Unlock()
		return out, nil
	}
	t.mu.Unlock()

	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case msg := <-rxCh:
		t.mu.Lock()
		out := t.stashAndTakeLocked(msg, maxBytes)
		t.mu.Unlock()
		return out, nil
	case <-timer.C:
		return []byte{}, nil
	case <-closed:
		_ = t.Disconnect(ctx)
		return nil, transports.ErrConnectionClosed
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

// stashAndTakeLocked returns up to maxBytes of msg, stashing any overflow.
func (t *PTYTransport) stashAndTakeLocked(msg []byte, maxBytes int) []byte {
	if maxBytes <= 0 || len(msg) <= maxBytes {
		return msg
	}
	out := make([]byte, maxBytes)
	copy(out, msg[:maxBytes])
	t.remnant = append(t.remnant, msg[maxBytes:]...)
	return out
}

// takeRemnantLocked returns up to maxBytes from the stashed remnant.
func (t *PTYTransport) takeRemnantLocked(maxBytes int) []byte {
	if maxBytes <= 0 || len(t.remnant) <= maxBytes {
		out := t.remnant
		t.remnant = nil
		return out
	}
	out := make([]byte, maxBytes)
	copy(out, t.remnant[:maxBytes])
	t.remnant = t.remnant[maxBytes:]
	return out
}

// IsConnected reports whether the PTY master is open.
func (t *PTYTransport) IsConnected() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.master != nil
}
