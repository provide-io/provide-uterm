//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

//go:build !linux

package pty

import "net"

// peerEUID reports that peer-credential auth is unavailable on this platform
// (SO_PEERCRED is Linux-only). Matches the Python behaviour of returning None on
// platforms without SO_PEERCRED (e.g. macOS), where the listener warns and
// falls back to the 0o600 socket permission as the access baseline.
func peerEUID(conn net.Conn) (int, bool) {
	return 0, false
}
