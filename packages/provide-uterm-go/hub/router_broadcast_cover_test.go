//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"errors"
	"testing"
)

// TestBroadcastHijackStatePrunesDeadBrowser drives BroadcastHijackState directly
// with a failing browser so its own dead-browser path runs: sendHijackStateTo
// reports the dead socket, RemoveDeadBrowsers prunes it, and the survivor pass
// re-sends state.
func TestBroadcastHijackStatePrunesDeadBrowser(t *testing.T) {
	h, _ := newTestHub(t, nil)
	good := newBrowserWS("good")
	bad := newBrowserWS("bad")
	bad.failSend = errors.New("boom")
	st := NewWorkerTermState()
	st.Browsers[good] = "viewer"
	st.Browsers[bad] = "operator"
	h.registry.Put("w1", st)

	if err := h.BroadcastHijackState(bg(), "w1"); err != nil {
		t.Fatalf("BroadcastHijackState: %v", err)
	}

	h.lock.Lock()
	_, badPresent := st.Browsers[bad]
	_, goodPresent := st.Browsers[good]
	h.lock.Unlock()
	if badPresent {
		t.Fatal("dead browser should have been pruned")
	}
	if !goodPresent {
		t.Fatal("good browser should be retained")
	}
}
