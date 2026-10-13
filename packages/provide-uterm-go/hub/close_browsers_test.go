//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "testing"

// sendOnlyBrowser is a browser conn with no close surface.
type sendOnlyBrowser struct{ name string }

func TestCloseBrowsersWhereClosesOnlyMatchingClosers(t *testing.T) {
	h, _ := newTestHub(t, nil)
	st := NewWorkerTermState()
	watcher := newBrowserWS("watcher")
	operator := newBrowserWS("operator")
	other := newBrowserWS("other-viewer")
	noCloser := &sendOnlyBrowser{name: "no-closer"}
	st.Browsers[watcher] = "viewer"
	st.Browsers[operator] = "operator"
	st.Browsers[other] = "viewer"
	st.Browsers[noCloser] = "viewer"
	h.registry.Put("w1", st)

	var seen []string
	closed := h.CloseBrowsersWhere(bg(), "w1", func(ws BrowserConn, role string) bool {
		seen = append(seen, role)
		return role == "viewer" && ws != other
	}, 4410, "viewer access revoked")

	if closed != 1 {
		t.Fatalf("closed = %d, want 1", closed)
	}
	// The conn with no close surface is skipped before match is asked about it.
	if len(seen) != 3 {
		t.Fatalf("match consulted %d times, want 3", len(seen))
	}
	if !watcher.closed || watcher.closeCode != 4410 || watcher.closeMsg != "viewer access revoked" {
		t.Fatalf("watcher close = %v %d %q", watcher.closed, watcher.closeCode, watcher.closeMsg)
	}
	if operator.closed || other.closed {
		t.Fatal("a non-matching browser was closed")
	}
}

func TestCloseBrowsersWhereUnknownWorkerClosesNothing(t *testing.T) {
	h, _ := newTestHub(t, nil)
	called := false
	if n := h.CloseBrowsersWhere(bg(), "missing", func(BrowserConn, string) bool { called = true; return true }, 4410, "x"); n != 0 {
		t.Fatalf("closed = %d", n)
	}
	if called {
		t.Fatal("match consulted for a worker with no state")
	}
}
