//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package tunnelclient

import (
	"os"
	"os/exec"
	"sync"

	"github.com/creack/pty"
	"golang.org/x/term"
)

// PTYSource is the read/write side of a terminal the share bridge pumps to and
// from the tunnel. Both SpawnedPty (spawned child PTY) and TtyProxy (local TTY
// in raw mode) satisfy it, so the bridge loop is source-agnostic:
//
//   - Read yields bytes to send to the remote (child output, or local stdin).
//   - Write receives bytes from the remote (child input, or local stdout echo).
type PTYSource interface {
	Read(p []byte) (int, error)
	Write(p []byte) (int, error)
	Close() error
}

// SpawnedPty is a child process running in a freshly-allocated PTY. It is the Go
// port of pty_capture.py's SpawnedPty, backed by github.com/creack/pty — the
// de-facto standard Go PTY library — which handles the openpty/fork/setsid/
// TIOCSCTTY dance portably across macOS and Linux (replacing Python's
// pty.fork()). Read/Write target the PTY master; the OS serializes those, and
// Close is guarded for idempotency.
type SpawnedPty struct {
	master *os.File
	cmd    *exec.Cmd

	mu     sync.Mutex
	closed bool
}

// SpawnPTY spawns command (defaulting to $SHELL, then /bin/sh) in a new PTY.
func SpawnPTY(command []string) (*SpawnedPty, error) {
	if len(command) == 0 {
		shell := os.Getenv("SHELL")
		if shell == "" {
			shell = "/bin/sh"
		}
		command = []string{shell}
	}
	c := exec.Command(command[0], command[1:]...) //nolint:gosec // user-chosen command, by design (== Python execvp)
	master, err := pty.Start(c)
	if err != nil {
		return nil, err
	}
	return &SpawnedPty{master: master, cmd: c}, nil
}

// Read reads from the PTY master.
func (s *SpawnedPty) Read(p []byte) (int, error) { return s.master.Read(p) }

// Write writes to the PTY master (delivered to the child's stdin).
func (s *SpawnedPty) Write(p []byte) (int, error) { return s.master.Write(p) }

// Closed reports whether Close has run.
func (s *SpawnedPty) Closed() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.closed
}

// Resize sets the PTY window size. It is a no-op once closed.
func (s *SpawnedPty) Resize(cols, rows int) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return nil
	}
	return pty.Setsize(s.master, &pty.Winsize{Rows: uint16(rows), Cols: uint16(cols)}) //nolint:gosec // sizes fit uint16
}

// TermSize returns the current (cols, rows), falling back to (80, 24).
func (s *SpawnedPty) TermSize() (cols, rows int) {
	ws, err := pty.GetsizeFull(s.master)
	if err != nil || ws.Cols == 0 || ws.Rows == 0 {
		return 80, 24
	}
	return int(ws.Cols), int(ws.Rows)
}

// Close closes the master fd and reaps the child. Idempotent. It kills the child
// (best-effort) then Waits so no zombie is left — the Go analogue of Python's
// os.close + waitpid(WNOHANG), but deterministic for tests.
func (s *SpawnedPty) Close() error {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil
	}
	s.closed = true
	s.mu.Unlock()

	_ = s.master.Close()
	if s.cmd.Process != nil {
		_ = s.cmd.Process.Kill()
		_ = s.cmd.Wait()
	}
	return nil
}

// TtyProxy puts the local TTY (stdin) into raw mode and proxies it. It is the Go
// port of pty_capture.py's TtyProxy, using golang.org/x/term for raw mode and
// size queries. Read yields local keystrokes; Write echoes remote bytes to
// stdout. Close restores the saved terminal state.
type TtyProxy struct {
	in  *os.File
	out *os.File

	mu       sync.Mutex
	oldState *term.State
	active   bool
}

// NewTtyProxy builds a proxy over os.Stdin/os.Stdout.
func NewTtyProxy() *TtyProxy {
	return &TtyProxy{in: os.Stdin, out: os.Stdout}
}

// Active reports whether the TTY is currently in raw mode.
func (t *TtyProxy) Active() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.active
}

// Start enters raw mode on stdin and returns the current (cols, rows). It errors
// if stdin is not a terminal, mirroring Python's OSError("stdin is not a TTY").
func (t *TtyProxy) Start() (cols, rows int, err error) {
	fd := int(t.in.Fd())
	if !term.IsTerminal(fd) {
		return 0, 0, os.ErrInvalid
	}
	state, err := term.MakeRaw(fd)
	if err != nil {
		return 0, 0, err
	}
	t.mu.Lock()
	t.oldState = state
	t.active = true
	t.mu.Unlock()
	cols, rows = t.TermSize()
	return cols, rows, nil
}

// Read reads local keystrokes from stdin.
func (t *TtyProxy) Read(p []byte) (int, error) { return t.in.Read(p) }

// Write echoes remote bytes to local stdout.
func (t *TtyProxy) Write(p []byte) (int, error) { return t.out.Write(p) }

// TermSize returns the local TTY (cols, rows), falling back to (80, 24).
func (t *TtyProxy) TermSize() (cols, rows int) {
	w, h, err := term.GetSize(int(t.in.Fd()))
	if err != nil || w == 0 || h == 0 {
		return 80, 24
	}
	return w, h
}

// Close restores the saved terminal state. Safe to call when not active.
func (t *TtyProxy) Close() error {
	t.mu.Lock()
	defer t.mu.Unlock()
	if !t.active {
		return nil
	}
	t.active = false
	if t.oldState != nil {
		return term.Restore(int(t.in.Fd()), t.oldState)
	}
	return nil
}
