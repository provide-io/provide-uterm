//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import "time"

// Frame is a worker-protocol frame. It mirrors the Python connector's plain
// dicts (e.g. {"type": "snapshot", ...}); a map keeps the wire shape and key
// set identical to the reference connector.py / capture_connector.py.
type Frame map[string]any

// nowTS returns the current Unix time in fractional seconds, matching Python's
// time.time().
func nowTS() float64 {
	return float64(time.Now().UnixNano()) / 1e9
}
