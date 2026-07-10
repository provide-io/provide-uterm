//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

//go:build unix

package manager

import "syscall"

// newSysProcAttr returns the attributes that place the child in its own
// session/process group, mirroring the POSIX start_new_session=True branch of
// _spawn_platform_kwargs. Worker rlimits are NOT applied in the child (Go
// cannot run an arbitrary preexec_fn); see the port notes.
func newSysProcAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{Setsid: true}
}

// signalGroupByPID signals the whole process group of pid, mirroring
// os.killpg(os.getpgid(pid), sig). Sending to -pgid targets the group.
func signalGroupByPID(pid int, sig syscall.Signal) error {
	pgid, err := syscall.Getpgid(pid)
	if err != nil {
		return err
	}
	return syscall.Kill(-pgid, sig)
}
