//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vt

import "testing"

// TestScreenBell exercises the Bell stub (no-op, must not panic).
func TestScreenBell(t *testing.T) {
	s := NewScreen(80, 24)
	s.Bell()
}
