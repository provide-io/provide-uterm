//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

// ForgetInputBuffer drops any partial command-accumulation buffer held for ws.
// Called from the browser-disconnect cleanup paths so a dropped browser never
// leaks its input buffer (the Go analogue of the Python
// “hub._input_buffers.pop(ws, None)“ in core_delegates_connection). Added as a
// wave-B extension method on the wave-A [StateStore] (same package) rather than
// modifying wave-A.
func (s *StateStore) ForgetInputBuffer(ws BrowserConn) {
	s.inputBuffersMu.Lock()
	defer s.inputBuffersMu.Unlock()
	delete(s.inputBuffers, ws)
}
