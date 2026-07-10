//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"os"
	"os/exec"
	"os/user"
	"strconv"
	"syscall"

	"github.com/creack/pty"
)

// spawnedChild is a running child in a freshly-allocated PTY.
type spawnedChild struct {
	master *os.File
	cmd    *exec.Cmd
}

// spawnPTY opens a PTY, disables local echo, optionally drops privileges to
// resolved, and execs command. Port of connector.py's pty.openpty + fork +
// _child_exec (setsid / TIOCSCTTY / setgid / initgroups / setuid / execve),
// realized with github.com/creack/pty (which performs the openpty + setsid +
// controlling-tty dance portably) plus syscall.Credential for the uid/gid drop.
//
// SECURITY: when resolved != nil the child runs with Credential{Uid,Gid,Groups}
// set, so setgroups+setgid+setuid are applied atomically in the child before
// exec. This only succeeds when the server runs as root (setuid/setgid require
// CAP_SETUID/CAP_SETGID); as a non-root user exec fails with EPERM and the
// error is surfaced — the child is NEVER launched with the caller's privileges
// when a drop was requested (fail-closed).
func spawnPTY(
	command string,
	args []string,
	env []string,
	resolved *ResolvedUser,
	cols, rows int,
) (*spawnedChild, error) {
	//nolint:gosec // command is a validated absolute path (ValidateCommand); by design.
	c := exec.Command(command, args...)
	c.Env = env

	attrs := &syscall.SysProcAttr{Setsid: true, Setctty: true}
	if resolved != nil {
		attrs.Credential = &syscall.Credential{
			Uid:    uint32(resolved.UID), //nolint:gosec // uid is a resolved OS uid
			Gid:    uint32(resolved.GID), //nolint:gosec // gid is a resolved OS gid
			Groups: supplementaryGroups(resolved),
		}
	}

	ws := &pty.Winsize{Rows: uint16(rows), Cols: uint16(cols)} //nolint:gosec // sizes fit uint16
	master, err := pty.StartWithAttrs(c, ws, attrs)
	if err != nil {
		return nil, err
	}

	// Disable local echo so server-written input is not reflected in the read
	// stream (the frontend renders its own echo). The termios state is shared
	// across the pty pair, so applying it to the master fd is equivalent to
	// tcsetattr on the slave.
	if err := disableEcho(int(master.Fd())); err != nil {
		_ = master.Close()
		if c.Process != nil {
			_ = c.Process.Kill()
			_, _ = c.Process.Wait()
		}
		return nil, err
	}

	return &spawnedChild{master: master, cmd: c}, nil
}

// supplementaryGroups resolves the supplementary group ids for resolved (the Go
// analogue of os.initgroups(name, gid)). Best-effort: on any lookup failure it
// returns nil, so Credential falls back to setgroups([]) — never leaving the
// caller's supplementary groups in place.
func supplementaryGroups(resolved *ResolvedUser) []uint32 {
	u, err := user.LookupId(strconv.Itoa(resolved.UID))
	if err != nil {
		return nil
	}
	ids, err := u.GroupIds()
	if err != nil {
		return nil
	}
	groups := make([]uint32, 0, len(ids)+1)
	seen := map[uint32]struct{}{}
	add := func(g int) {
		gg := uint32(g) //nolint:gosec // gid is a resolved OS gid
		if _, ok := seen[gg]; ok {
			return
		}
		seen[gg] = struct{}{}
		groups = append(groups, gg)
	}
	add(resolved.GID)
	for _, s := range ids {
		if g, convErr := strconv.Atoi(s); convErr == nil {
			add(g)
		}
	}
	return groups
}
