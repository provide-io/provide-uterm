//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package embed

import "testing"

// TestMarkDetached covers markDetached, which is only reachable via a race in
// ClientHandle.Receive (every channel-close site sets slot.closed first, so the
// early-return normally wins). Invoke it directly to assert it flips the flags.
func TestMarkDetached(t *testing.T) {
	h := NewHub()
	s, err := h.CreateSession(Options{SessionID: "md"})
	if err != nil {
		t.Fatal(err)
	}
	c, err := s.AttachClient(ClientMetadata{ClientID: "c1"})
	if err != nil {
		t.Fatal(err)
	}
	if !c.IsAttached() {
		t.Fatal("client should be attached after AttachClient")
	}

	c.markDetached()

	if c.IsAttached() {
		t.Fatal("markDetached must clear the attached flag")
	}
	if !c.slot.closed.Load() {
		t.Fatal("markDetached must mark the slot closed")
	}
}
