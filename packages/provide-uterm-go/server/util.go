//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import "strconv"

// itoa is a short alias for strconv.Itoa used in error messages.
func itoa(v int) string { return strconv.Itoa(v) }

// clampInt clamps v into the inclusive [min,max] range.
func clampInt(v, minV, maxV int) int {
	if v < minV {
		return minV
	}
	if v > maxV {
		return maxV
	}
	return v
}

// truncate returns s limited to at most n bytes (used for event previews).
func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
