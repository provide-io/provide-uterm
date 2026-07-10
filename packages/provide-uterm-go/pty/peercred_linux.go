//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

//go:build linux

package pty

import (
	"net"

	"golang.org/x/sys/unix"
)

// peerEUID returns the connecting peer's uid via SO_PEERCRED, or ok=false when
// unavailable. Port of PamNotifyListener._peer_euid (Linux SO_PEERCRED path).
func peerEUID(conn net.Conn) (int, bool) {
	uc, ok := conn.(*net.UnixConn)
	if !ok {
		return 0, false
	}
	raw, err := uc.SyscallConn()
	if err != nil {
		return 0, false
	}
	var uid int
	var innerErr error
	ctrlErr := raw.Control(func(fd uintptr) {
		cred, e := unix.GetsockoptUcred(int(fd), unix.SOL_SOCKET, unix.SO_PEERCRED)
		if e != nil {
			innerErr = e
			return
		}
		uid = int(cred.Uid)
	})
	if ctrlErr != nil || innerErr != nil {
		return 0, false
	}
	return uid, true
}
