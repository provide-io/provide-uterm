//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

// SessionLifecycleState is what a hosted session's runtime reports on the wire,
// as `lifecycle_state` on every session object.
//
// The vocabulary is the reference's session-runtime one, and only that one:
// `SessionLifecycle` in bridge/contracts.py, assigned by
// HostedSessionRuntime in provide-uterm-server/.../runtime.py.
//
// It is NOT the control plane's LifecycleState
// (waiting|running|stopped|error|deleted, control/plane/session/types.py).
// That is a persisted record store for a different subsystem; it shares the
// field *name* and nothing else, and it never reaches this field. This port
// used to report its "waiting" here, which is a word no client of the
// reference knows how to read. See controlplane.SessionRecord.LifecycleState
// for the record-store vocabulary, which stays as it is.
//
// The names are declared once, here, so a fifth vocabulary cannot be
// introduced by a typo somewhere down the call graph.
type SessionLifecycleState string

const (
	// LifecycleStopped: registered but never brought up, brought down again, or
	// given up on after a start that failed — in that last case last_error says
	// what failed and stopped_at says when. The state alone does not distinguish
	// them, and in the reference it never did.
	LifecycleStopped SessionLifecycleState = "stopped"
	// LifecycleStarting: asked to come up; the connector has not reported in yet.
	LifecycleStarting SessionLifecycleState = "starting"
	// LifecycleRunning: up.
	LifecycleRunning SessionLifecycleState = "running"
	// LifecycleError: a run failed and another attempt is coming. In the
	// reference this is set inside HostedSessionRuntime._run's retry loop
	// (runtime.py, ~443) and is only observable between attempts: a permanent
	// failure breaks out of the loop and the following line assigns "stopped",
	// and a transient one loops back round to "starting". It is not a resting
	// state, and this port — which has no retry loop — therefore never assigns
	// it. It stays in the vocabulary because it is in the reference's, and a
	// client reading this field must be able to name what it may be sent.
	LifecycleError SessionLifecycleState = "error"
)

// SessionLifecycleStates is every name, in the reference's declaration order.
var SessionLifecycleStates = []SessionLifecycleState{
	LifecycleStopped,
	LifecycleStarting,
	LifecycleRunning,
	LifecycleError,
}

// Valid reports whether s is one of the four names a client can read.
func (s SessionLifecycleState) Valid() bool {
	switch s {
	case LifecycleStopped, LifecycleStarting, LifecycleRunning, LifecycleError:
		return true
	default:
		return false
	}
}
