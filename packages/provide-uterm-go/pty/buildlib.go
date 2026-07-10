//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

// getCaptureLibPath returns the path to the libuterm_capture LD_PRELOAD/DYLD
// shared library, or "" when it is not available.
//
// PLATFORM STUB (not portable to Go): libuterm_capture is a C library injected
// into the target process via LD_PRELOAD (Linux) / DYLD_INSERT_LIBRARIES
// (macOS) to intercept write()/connect() at the libc boundary. That is C build
// tooling with no Go equivalent, so this port does not ship the library and
// always returns "". The connector-side machinery (creating the capture socket,
// exporting UTERM_CAPTURE_SOCKET, and reading captured frames — see capture.go
// and CaptureConnector) IS ported and fully exercised; only the C injection
// artifact is absent. With no library path the connector simply does not set
// LD_PRELOAD/DYLD, exactly as the Python path behaves when the library was not
// built (_build.get_capture_lib_path → None).
func getCaptureLibPath() string {
	return ""
}
