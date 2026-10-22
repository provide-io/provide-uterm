//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

//go:build linux || darwin

package pty

import "golang.org/x/sys/unix"

// umaskSet sets the process umask and returns the previous value. It wraps the
// platform syscall so the capture / notify sockets can be bound under the
// umask their mode needs. The process umask is global: bind through
// listenUnderUmask, which holds bindUmaskMu around set, bind and restore. A
// variable so a test can see which umask a bind ran under.
var umaskSet = unix.Umask
