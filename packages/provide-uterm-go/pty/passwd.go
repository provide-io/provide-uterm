//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"bufio"
	"os"
	"strconv"
	"strings"
)

// defaultShell is the fallback login shell used when the login shell cannot be
// determined (see lookupShell). Matches the synthetic-user default in uid_map.py.
const defaultShell = "/bin/sh"

// lookupShell returns the login shell for the given uid / username.
//
// DEVIATION FROM PYTHON: pwd.getpwnam/getpwuid expose pw_shell via getpwnam(3),
// which consults the platform name service (Directory Services on macOS,
// /etc/passwd/NSS on Linux). Go's os/user resolves uid/gid/home/name the same
// way but deliberately does NOT expose the login shell. To recover it without
// cgo we parse /etc/passwd directly — authoritative on Linux (where CI runs),
// but empty on macOS where users live in Directory Services. When the entry is
// not found we fall back to $SHELL then /bin/sh, so the resolved user always has
// a usable shell. The uid/gid/home/name fields remain fully faithful.
func lookupShell(uid int, name string) string {
	if shell := shellFromPasswd(uid, name); shell != "" {
		return shell
	}
	if shell := os.Getenv("SHELL"); shell != "" {
		return shell
	}
	return defaultShell
}

// shellFromPasswd scans /etc/passwd for a line matching name (or uid) and
// returns its shell field, or "" when no match / the file is unreadable.
func shellFromPasswd(uid int, name string) string {
	f, err := os.Open("/etc/passwd")
	if err != nil {
		return ""
	}
	defer func() { _ = f.Close() }()

	uidStr := strconv.Itoa(uid)
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		// name:passwd:uid:gid:gecos:home:shell
		fields := strings.Split(line, ":")
		if len(fields) < 7 {
			continue
		}
		if (name != "" && fields[0] == name) || fields[2] == uidStr {
			return fields[6]
		}
	}
	return ""
}
