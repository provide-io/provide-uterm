//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"errors"
	"os/exec"
	"sync"
	"syscall"
	"time"
)

// errWaitTimeout is returned by processHandle.WaitExit when the process does
// not exit within the deadline, mirroring Python's asyncio.TimeoutError.
var errWaitTimeout = errors.New("process did not exit within timeout")

// processHandle abstracts a spawned OS process so tests can inject fakes in
// place of a real child. It mirrors the subset of subprocess.Popen the manager
// relies on: pid, poll(), wait(timeout), and killpg.
type processHandle interface {
	// PID returns the child's process id.
	PID() int
	// Poll returns (returncode, exited). exited is false while running,
	// mirroring Popen.poll() returning None.
	Poll() (int, bool)
	// WaitExit blocks until the process exits or timeout elapses (returns
	// errWaitTimeout on timeout).
	WaitExit(timeout time.Duration) error
	// SignalGroup sends sig to the process group, mirroring
	// os.killpg(os.getpgid(pid), sig).
	SignalGroup(sig syscall.Signal) error
}

// managedProcess is the real processHandle backed by an *exec.Cmd. A single
// reaper goroutine calls Wait() exactly once and records the exit code.
type managedProcess struct {
	pid int
	cmd *exec.Cmd

	mu       sync.Mutex
	exited   bool
	exitCode int
	done     chan struct{}
}

// newManagedProcess starts the reaper goroutine for an already-started cmd.
func newManagedProcess(cmd *exec.Cmd) *managedProcess {
	p := &managedProcess{pid: cmd.Process.Pid, cmd: cmd, done: make(chan struct{})}
	go p.reap()
	return p
}

// reap waits for the child and records its exit code.
func (p *managedProcess) reap() {
	err := p.cmd.Wait()
	code := 0
	if err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			// ExitCode() is -1 when the process was terminated by a signal,
			// which the manager treats as a non-zero (error) exit.
			code = ee.ExitCode()
		} else {
			code = -1
		}
	}
	p.mu.Lock()
	p.exited = true
	p.exitCode = code
	p.mu.Unlock()
	close(p.done)
}

// PID returns the child pid.
func (p *managedProcess) PID() int { return p.pid }

// Poll returns (returncode, exited).
func (p *managedProcess) Poll() (int, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.exitCode, p.exited
}

// WaitExit blocks until exit or timeout.
func (p *managedProcess) WaitExit(timeout time.Duration) error {
	select {
	case <-p.done:
		return nil
	case <-time.After(timeout):
		return errWaitTimeout
	}
}

// SignalGroup signals the child's process group.
func (p *managedProcess) SignalGroup(sig syscall.Signal) error {
	return signalGroupByPID(p.pid, sig)
}
