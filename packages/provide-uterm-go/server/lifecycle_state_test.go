//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"encoding/json"
	"testing"
)

// The wire vocabulary is the reference's session-runtime one — the four names
// bridge/contracts.py's SessionLifecycle spells, in its order. It is NOT the
// control plane's (waiting|running|stopped|error|deleted), which names rows in
// a persisted record store and never reaches this field.
func TestSessionLifecycleStatesAreTheReferenceVocabulary(t *testing.T) {
	want := []SessionLifecycleState{"stopped", "starting", "running", "error"}
	if len(SessionLifecycleStates) != len(want) {
		t.Fatalf("SessionLifecycleStates = %v, want %v", SessionLifecycleStates, want)
	}
	for i, state := range want {
		if SessionLifecycleStates[i] != state {
			t.Fatalf("SessionLifecycleStates[%d] = %q, want %q", i, SessionLifecycleStates[i], state)
		}
	}
	named := []SessionLifecycleState{
		LifecycleStopped, LifecycleStarting, LifecycleRunning, LifecycleError,
	}
	for i, state := range named {
		if state != want[i] {
			t.Fatalf("named constant %d = %q, want %q", i, state, want[i])
		}
	}
}

// "waiting" is the control plane's word. A port reporting it on this field is
// speaking a vocabulary no client of the reference knows how to read.
func TestSessionLifecycleStateRejectsTheControlPlaneVocabulary(t *testing.T) {
	for _, bad := range []SessionLifecycleState{"waiting", "deleted", "", "Running"} {
		if bad.Valid() {
			t.Fatalf("%q must not be a valid session lifecycle state", bad)
		}
	}
	for _, good := range SessionLifecycleStates {
		if !good.Valid() {
			t.Fatalf("%q must be a valid session lifecycle state", good)
		}
	}
}

// The state is a bare JSON string on the wire, not an object or a number.
func TestSessionLifecycleStateSerializesAsAString(t *testing.T) {
	raw, err := json.Marshal(&SessionStatus{LifecycleState: LifecycleRunning})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if decoded["lifecycle_state"] != "running" {
		t.Fatalf("lifecycle_state = %#v, want the string \"running\"", decoded["lifecycle_state"])
	}
}
