//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

// stubPamBackend is the default PAM backend: it refuses to authenticate.
//
// PLATFORM STUB: Go has no standard-library PAM binding, and this port does not
// pull in a cgo libpam dependency. The stub returns the same "libpam not
// available on this system" error the Python code raises when
// ctypes.util.find_library("pam") returns None — a fail-closed default. To run
// real PAM authentication, provide a build-tagged cgo backend implementing
// pamBackend (pam_start/pam_authenticate/pam_acct_mgmt/pam_open_session/
// pam_getenvlist/pam_close_session/pam_end) and inject it via
// NewPamSessionWithBackend. This stub is deliberately the only default so that a
// build without an explicit backend can never silently accept credentials.
type stubPamBackend struct{}

// Authenticate always fails — no libpam available. Fail-closed.
func (stubPamBackend) Authenticate(service, username, password string) (pamHandle, error) {
	return nil, newPamError("libpam not available on this system")
}

// defaultPamBackend returns the process-wide default backend (the fail-closed
// stub). A real deployment overrides per-session via NewPamSessionWithBackend.
func defaultPamBackend() pamBackend {
	return stubPamBackend{}
}
