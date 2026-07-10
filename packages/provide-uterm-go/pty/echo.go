//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

//go:build linux || darwin

package pty

import "golang.org/x/sys/unix"

// disableEcho clears the ECHO local-flag on the tty referenced by fd so input
// written by the server is not reflected back in the master's read stream (the
// frontend renders its own echo). Port of connector.py's
// termios.tcgetattr → clear ECHO → tcsetattr on the PTY slave; the termios
// state is shared across the pty pair, so applying it to the master fd is
// equivalent.
func disableEcho(fd int) error {
	t, err := getTermios(fd)
	if err != nil {
		return err
	}
	t.Lflag &^= unix.ECHO
	return setTermios(fd, t)
}
