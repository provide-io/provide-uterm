//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package screen

import (
	"bytes"
	"testing"
)

func TestDecodeCP437(t *testing.T) {
	t.Run("ascii range", func(t *testing.T) {
		if got := DecodeCP437([]byte("hello world")); got != "hello world" {
			t.Errorf("got %q", got)
		}
	})
	t.Run("box drawing", func(t *testing.T) {
		if got := DecodeCP437([]byte{0xC4}); got != "─" {
			t.Errorf("0xC4 = %q, want ─", got)
		}
	})
	t.Run("six box drawing runes", func(t *testing.T) {
		got := DecodeCP437([]byte{0xC4, 0xB3, 0xDA, 0xBF, 0xC0, 0xD9})
		if got != "─│┌┐└┘" {
			t.Errorf("got %q, want ─│┌┐└┘", got)
		}
	})
	t.Run("high range samples", func(t *testing.T) {
		samples := map[byte]string{
			0x80: "Ç", 0x9B: "¢", 0xB0: "░", 0xDB: "█",
			0xE0: "α", 0xEE: "ε", 0xF6: "÷", 0xFF: "\u00a0",
		}
		for b, want := range samples {
			if got := DecodeCP437([]byte{b}); got != want {
				t.Errorf("DecodeCP437(0x%02X) = %q, want %q", b, got, want)
			}
		}
	})
	t.Run("empty", func(t *testing.T) {
		if got := DecodeCP437(nil); got != "" {
			t.Errorf("got %q", got)
		}
	})
}

func TestEncodeCP437(t *testing.T) {
	t.Run("ascii range", func(t *testing.T) {
		if got := EncodeCP437("hello"); !bytes.Equal(got, []byte("hello")) {
			t.Errorf("got %v", got)
		}
	})
	t.Run("unencodable replaced with question mark", func(t *testing.T) {
		got := EncodeCP437("hello \U0001F600")
		if !bytes.Equal(got, []byte("hello ?")) {
			t.Errorf("got %v, want %q", got, "hello ?")
		}
	})
	t.Run("box drawing round trip", func(t *testing.T) {
		original := "─│┌┐└┘"
		if got := DecodeCP437(EncodeCP437(original)); got != original {
			t.Errorf("round trip = %q, want %q", got, original)
		}
	})
	t.Run("invalid utf8 replaced", func(t *testing.T) {
		// A bare 0xFF byte is invalid UTF-8; range yields U+FFFD → '?'.
		if got := EncodeCP437(string([]byte{0xFF})); !bytes.Equal(got, []byte("?")) {
			t.Errorf("got %v, want ?", got)
		}
	})
}

// TestCP437FullTableRoundTrip verifies DecodeCP437/EncodeCP437 are exact
// inverses over all 256 byte values, and pins spot values against CPython's
// cp437 codec (see the generation note in cp437.go; the full table was also
// differentially verified against Python via a scratch corpus test).
func TestCP437FullTableRoundTrip(t *testing.T) {
	all := make([]byte, 256)
	for i := range all {
		all[i] = byte(i)
	}
	decoded := DecodeCP437(all)
	runes := []rune(decoded)
	if len(runes) != 256 {
		t.Fatalf("decoded length %d, want 256", len(runes))
	}
	// Bytes 0x00-0x7F map to themselves (ASCII, including control chars).
	for i := range 128 {
		if runes[i] != rune(i) {
			t.Errorf("byte 0x%02X decoded to U+%04X, want U+%04X", i, runes[i], i)
		}
	}
	// The mapping is bijective.
	seen := map[rune]bool{}
	for i, r := range runes {
		if seen[r] {
			t.Errorf("byte 0x%02X decodes to duplicate rune U+%04X", i, r)
		}
		seen[r] = true
	}
	// Round trip: encode(decode(b)) == b for every byte.
	if got := EncodeCP437(decoded); !bytes.Equal(got, all) {
		t.Errorf("encode(decode(all bytes)) diverged: %v", got)
	}
}
