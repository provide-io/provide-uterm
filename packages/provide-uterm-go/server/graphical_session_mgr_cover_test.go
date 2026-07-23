//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/gui"
)

type recordingCloser struct{ closed *bool }

func (c recordingCloser) Close() error { *c.closed = true; return nil }

// TestGraphicalSessionManagerNilSession covers the no-session error branches.
func TestGraphicalSessionManagerNilSession(t *testing.T) {
	m := NewGraphicalSessionManager()
	if _, err := m.Screenshot(); err == nil {
		t.Fatal("Screenshot on empty manager must error")
	}
	if err := m.InjectPointer(1, 1, 1); err == nil {
		t.Fatal("InjectPointer on empty manager must error")
	}
	if err := m.InjectKey(1, true); err == nil {
		t.Fatal("InjectKey on empty manager must error")
	}
	// Close with nothing attached is a no-op.
	if err := m.Close(); err != nil {
		t.Fatalf("Close empty: %v", err)
	}
}

// TestGraphicalSessionManagerLifecycle covers Attach, the live-session op paths,
// Close (cancel + closer), Replace, and Detach.
func TestGraphicalSessionManagerLifecycle(t *testing.T) {
	m := NewGraphicalSessionManager()
	sess := gui.NewMemoryGraphicalSession(8, 8)

	closed := false
	cancelled := false
	m.Attach(sess, recordingCloser{&closed}, func() { cancelled = true })

	if _, err := m.Screenshot(); err != nil {
		t.Fatalf("Screenshot live: %v", err)
	}
	if err := m.InjectPointer(2, 2, 1); err != nil {
		t.Fatalf("InjectPointer live: %v", err)
	}
	if err := m.InjectKey(0x41, true); err != nil {
		t.Fatalf("InjectKey live: %v", err)
	}

	// Replace closes the current session's cancel + closer, then attaches anew.
	sess2 := gui.NewMemoryGraphicalSession(4, 4)
	closed2 := false
	cancelled2 := false
	m.Replace(sess2, recordingCloser{&closed2}, func() { cancelled2 = true })
	if !closed || !cancelled {
		t.Fatalf("Replace must close previous session: closed=%v cancelled=%v", closed, cancelled)
	}

	// Detach closes the second session and clears state.
	m.Detach()
	if !closed2 || !cancelled2 {
		t.Fatalf("Detach must close active session: closed=%v cancelled=%v", closed2, cancelled2)
	}
	if _, err := m.Screenshot(); err == nil {
		t.Fatal("Screenshot after Detach must error")
	}
}
