//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package pty is the Go port of provide.uterm.pty (the provide-uterm platform
// PTY connector). It spawns a local pseudo-terminal, supervises the child, and
// exposes the session-connector method set the server's connector registry
// consumes (Start/Stop/IsConnected, PollMessages, HandleInput, HandleControl,
// GetSnapshot, GetAnalysis, SetMode, Clear) — the same surface as the reference
// UshellConnector.
//
// Module map (which Python module each Go file ports):
//
//   - validate.go          ← _validate.py    (input validation, fully ported)
//   - socketutil.go        ← socket_utils.py (fully ported)
//   - uidmap.go/passwd.go  ← uid_map.py      (ported; see passwd.go for the
//     macOS shell-lookup deviation — os/user does not expose the login shell)
//   - connector.go/spawn.go ← connector.py   (ported; setuid/setgid privilege
//     drop is exercised only as root — platform-gated)
//   - capture.go           ← capture.py      (capture socket reader, fully ported)
//   - captureconnector.go  ← capture_connector.py (fully ported)
//   - buildlib.go          ← _build.py       (LD_PRELOAD/DYLD capture C library
//     is not portable Go; getCaptureLibPath is a documented stub)
//   - pam.go/pambackend.go ← pam.py          (lifecycle ported; the libpam C call
//     is gated behind the pamBackend interface — the default backend is a
//     fail-closed stub, matching the Python "libpam not available" path)
//   - pamlistener.go       ← pam_listener.py (notify-socket protocol, ported;
//     SO_PEERCRED peer-uid check is Linux-only, as in Python)
//
// Platform notes and security invariants live next to each affected symbol.
package pty
