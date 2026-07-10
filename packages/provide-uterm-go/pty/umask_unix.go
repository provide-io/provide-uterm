//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

//go:build linux || darwin

package pty

import "golang.org/x/sys/unix"

// umaskSet sets the process umask and returns the previous value. It wraps the
// platform syscall so the capture / notify sockets can be bound 0o600 under a
// restrictive umask. NOTE: the process umask is global and not goroutine-safe —
// callers must keep the set→bind→restore window as tight as possible.
func umaskSet(mask int) int {
	return unix.Umask(mask)
}
