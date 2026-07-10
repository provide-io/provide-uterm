//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import "time"

// Protocol-version range advertised in worker_hello frames. These mirror
// provide.uterm.bridge.contracts (MIN/MAX/PREFERRED_PROTOCOL_VERSION), which
// are all 1 today. Kept local so the shell package stays self-contained.
const (
	minProtocolVersion       = 1
	maxProtocolVersion       = 1
	preferredProtocolVersion = 1
)

// Frame is a worker-protocol frame. It mirrors the Python dicts produced by
// terminal/_output.py (e.g. {"type": "term", "data": ..., "ts": ...}); using a
// map keeps the wire shape and key set identical to the reference.
type Frame map[string]any

// nowTS returns the current Unix time in fractional seconds, matching Python's
// time.time().
func nowTS() float64 {
	return float64(time.Now().UnixNano()) / 1e9
}

// Term builds a "term" worker-protocol frame. Port of terminal/_output.term.
func Term(data string) Frame {
	return Frame{"type": "term", "data": data, "ts": nowTS()}
}

// TermAt builds a "term" frame with an explicit timestamp; ts <= 0 means "now"
// (mirroring the Python default-argument behaviour where ts or time.time()).
func TermAt(data string, ts float64) Frame {
	if ts <= 0 {
		ts = nowTS()
	}
	return Frame{"type": "term", "data": data, "ts": ts}
}

// WorkerHello builds a "worker_hello" frame declaring the session input mode
// and the advertised protocol-version range. Port of
// terminal/_output.worker_hello.
func WorkerHello(inputMode string) Frame {
	return Frame{
		"type":       "worker_hello",
		"input_mode": inputMode,
		"ts":         nowTS(),
		"protocol": map[string]int{
			"min":       minProtocolVersion,
			"max":       maxProtocolVersion,
			"preferred": preferredProtocolVersion,
		},
	}
}
