//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vt

import "testing"

func TestHasModeAndCharsets(t *testing.T) {
	s := NewScreen(10, 3)
	if !s.HasMode(DECAWM) || s.HasMode(IRM) {
		t.Fatalf("power-on modes %v", s.Modes())
	}
	if g0, g1, shifted := s.Charsets(); g0 != "B" || g1 != "0" || shifted || s.CharsetsDesignated() {
		t.Fatalf("power-on charsets %q %q %v", g0, g1, shifted)
	}
	stream := NewStream(s)
	stream.UseUTF8 = false
	stream.Feed("\x1b(U\x1b)V\x0e\x1b[4h")
	if !s.HasMode(IRM) {
		t.Fatal("IRM not set")
	}
	if g0, g1, shifted := s.Charsets(); g0 != "U" || g1 != "V" || !shifted || !s.CharsetsDesignated() {
		t.Fatalf("charsets %q %q %v", g0, g1, shifted)
	}
}
