//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package ansi

import (
	"strings"
	"testing"
)

func TestHandlePipeCodesExact(t *testing.T) {
	tests := []struct {
		in   string
		want string
	}{
		{"|00", "\x1b[30m"},
		{"|01", "\x1b[34m"},
		{"|02", "\x1b[32m"},
		{"|03", "\x1b[36m"},
		{"|04", "\x1b[31m"},
		{"|05", "\x1b[35m"},
		{"|06", "\x1b[33m"},
		{"|07", "\x1b[37m"},
		{"|08", "\x1b[90m"},
		{"|09", "\x1b[94m"},
		{"|10", "\x1b[92m"},
		{"|11", "\x1b[96m"},
		{"|12", "\x1b[91m"},
		{"|13", "\x1b[95m"},
		{"|14", "\x1b[93m"},
		{"|15", "\x1b[97m"},
		{"|16", "\x1b[40m"},
		{"|17", "\x1b[44m"},
		{"|18", "\x1b[42m"},
		{"|19", "\x1b[46m"},
		{"|20", "\x1b[41m"},
		{"|21", "\x1b[45m"},
		{"|22", "\x1b[43m"},
		{"|23", "\x1b[47m"},
		{"|24", "|24"},         // out of range — passthrough
		{"|99", "|99"},         // out of range — passthrough
		{"|4 text", "|4 text"}, // single digit — not a pipe code
		{"|234", "\x1b[47m4"},  // greedy two-digit match, trailing digit kept
		{"plain", "plain"},
	}
	for _, tt := range tests {
		if got := handlePipeCodes(tt.in); got != tt.want {
			t.Errorf("handlePipeCodes(%q) = %q, want %q", tt.in, got, tt.want)
		}
	}
}

func TestHandlePipeCodesMixedWithText(t *testing.T) {
	got := handlePipeCodes("|04Red |02Green")
	for _, want := range []string{"\x1b[31m", "\x1b[32m", "Red ", "Green"} {
		if !strings.Contains(got, want) {
			t.Errorf("handlePipeCodes mixed output %q missing %q", got, want)
		}
	}
}

func TestHandleTildeCodesExact(t *testing.T) {
	tests := []struct {
		in   string
		want string
	}{
		{"~1", "\x1b[0;1;32m"},
		{"~2", "\x1b[0;1;37m"},
		{"~3", "\x1b[0;1;36m"},
		{"~4", "\x1b[0;1;31m"},
		{"~5", "\x1b[0;1;35m"},
		{"~6", "\x1b[0;1;33m"},
		{"~7", "\x1b[0;37m"},
		{"~0", "\x1b[0m"},
		{"~r", "\x1b[0;1;31m"},
		{"~R", "\x1b[0;1;31m"},
		{"~g", "\x1b[0;1;32m"},
		{"~G", "\x1b[0;1;32m"},
		{"~y", "\x1b[0;1;33m"},
		{"~Y", "\x1b[0;1;33m"},
		{"~b", "\x1b[0;1;34m"},
		{"~B", "\x1b[0;1;34m"},
		{"~m", "\x1b[0;1;35m"},
		{"~M", "\x1b[0;1;35m"},
		{"~c", "\x1b[0;1;36m"},
		{"~C", "\x1b[0;1;36m"},
		{"~w", "\x1b[0;1;37m"},
		{"~W", "\x1b[0;1;37m"},
		{"~d", "\x1b[0;37m"},
		{"~D", "\x1b[0;37m"},
		{"~E", "\x1b[0;1;31m"},
		{"~", "~"},         // trailing tilde — no match
		{"~Z", "~Z"},       // unknown code — passthrough
		{"~x", "~x"},       // "x" is not a tilde code
		{"A~ZB", "A~ZB"},   // unknown code in context
		{"~~1", "~~1"},     // "~~" consumes both tildes, leaves "1"
		{"plain", "plain"}, // no tildes
	}
	for _, tt := range tests {
		if got := handleTildeCodes(tt.in); got != tt.want {
			t.Errorf("handleTildeCodes(%q) = %q, want %q", tt.in, got, tt.want)
		}
	}
}

func TestHandleBraceTokensExact(t *testing.T) {
	tests := []struct {
		in   string
		want string
	}{
		{"{+c}", "\x1b[1;36m"},
		{"{-c}", "\x1b[0;36m"},
		{"{+r}", "\x1b[1;31m"},
		{"{-r}", "\x1b[0;31m"},
		{"{+g}", "\x1b[1;32m"},
		{"{-g}", "\x1b[0;32m"},
		{"{+y}", "\x1b[1;33m"},
		{"{-y}", "\x1b[0;33m"},
		{"{+b}", "\x1b[1;34m"},
		{"{-b}", "\x1b[0;34m"},
		{"{+m}", "\x1b[1;35m"},
		{"{-m}", "\x1b[0;35m"},
		{"{+w}", "\x1b[1;37m"},
		{"{+Bw}", "\x1b[1;37m"},
		{"{-w}", "\x1b[0;37m"},
		{"{+k}", "\x1b[1;30m"},
		{"{-k}", "\x1b[0;30m"},
		{"{-x}", "\x1b[0m"},
		{"{NK}", "\x1b[0m"},
		{"{T}", "\x1b[1m"},
		{"{t}", "\x1b[0m"},
		{"{+z}", "{+z}"},   // matches the 3-char pattern but unmapped
		{"{-Bw}", "{-Bw}"}, // matches the 4-char pattern but unmapped
		{"{+r", "{+r"},     // truncated — no closing brace
		{"{", "{"},
		{"{xr}", "{xr}"}, // invalid polarity
		{"A{+r}B", "A\x1b[1;31mB"},
		{"{+B}{+Bw}", "{+B}\x1b[1;37m"},
		{"plain", "plain"},
	}
	for _, tt := range tests {
		if got := handleBraceTokens(tt.in); got != tt.want {
			t.Errorf("handleBraceTokens(%q) = %q, want %q", tt.in, got, tt.want)
		}
	}
}

func TestHandleExtendedTokensExact(t *testing.T) {
	tests := []struct {
		in   string
		want string
	}{
		{"{P0}", "\x1b[30m"},
		{"{P7}", "\x1b[37m"},
		{"{P8}", "\x1b[90m"},
		{"{P15}", "\x1b[97m"},
		{"{P16}", "\x1b[30m"}, // modulo wraps
		{"{T0}", "\x1b[40m"},
		{"{T7}", "\x1b[47m"},
		{"{T8}", "\x1b[100m"},
		{"{T15}", "\x1b[107m"},
		{"{T16}", "\x1b[40m"}, // modulo wraps
		{"{F0}", "\x1b[38;5;0m"},
		{"{F196}", "\x1b[38;5;196m"},
		{"{F255}", "\x1b[38;5;255m"},
		{"{F300}", "\x1b[38;5;300m"}, // ≥256 falls back to formatted value
		{"{F007}", "\x1b[38;5;7m"},   // leading zeros normalized
		{"{B0}", "\x1b[48;5;0m"},
		{"{B45}", "\x1b[48;5;45m"},
		{"{B300}", "\x1b[48;5;300m"},
		{"{F1234}", "{F1234}"}, // >3 digits — no match
		{"{Fx}", "{Fx}"},
		{"a{F1}b{B2}c", "a\x1b[38;5;1mb\x1b[48;5;2mc"},
		{"plain", "plain"},
	}
	for _, tt := range tests {
		if got := handleExtendedTokens(tt.in); got != tt.want {
			t.Errorf("handleExtendedTokens(%q) = %q, want %q", tt.in, got, tt.want)
		}
	}
}

func TestEmitColorExact(t *testing.T) {
	tests := []struct {
		polarity, colorChar, want string
	}{
		{"-", "x", "\x1b[0m"},
		{"+", "x", "\x1b[0m"}, // "x" resets regardless of polarity
		{"+", "r", "\x1b[0;1;31m"},
		{"-", "r", "\x1b[0;31m"},
		{"+", "z", ""}, // unknown color char
	}
	for _, tt := range tests {
		if got := emitColor(tt.polarity, tt.colorChar); got != tt.want {
			t.Errorf("emitColor(%q, %q) = %q, want %q", tt.polarity, tt.colorChar, got, tt.want)
		}
	}
}

func TestNormalizeColors(t *testing.T) {
	t.Run("tilde codes", func(t *testing.T) {
		got := NormalizeColors("~1text~0")
		if !strings.Contains(got, "\x1b[") || !strings.Contains(got, "text") {
			t.Fatalf("got %q", got)
		}
		if strings.Contains(got, "~1") || strings.Contains(got, "~0") {
			t.Fatalf("tilde codes left in %q", got)
		}
	})
	t.Run("P token", func(t *testing.T) {
		if got := NormalizeColors("{P3}text"); got != "\x1b[33mtext" {
			t.Fatalf("got %q", got)
		}
	})
	t.Run("T token", func(t *testing.T) {
		if got := NormalizeColors("{T3}"); got != "\x1b[43m" {
			t.Fatalf("got %q", got)
		}
	})
	t.Run("F token", func(t *testing.T) {
		if got := NormalizeColors("{F196}text"); got != "\x1b[38;5;196mtext" {
			t.Fatalf("got %q", got)
		}
	})
	t.Run("B token", func(t *testing.T) {
		if got := NormalizeColors("{B45}text"); got != "\x1b[48;5;45mtext" {
			t.Fatalf("got %q", got)
		}
	})
	t.Run("plain passthrough", func(t *testing.T) {
		if got := NormalizeColors("no tokens here"); got != "no tokens here" {
			t.Fatalf("got %q", got)
		}
	})
	t.Run("pipe codes", func(t *testing.T) {
		got := NormalizeColors("|04red|00")
		if !strings.Contains(got, "\x1b[31m") || !strings.Contains(got, "\x1b[30m") {
			t.Fatalf("got %q", got)
		}
	})
	t.Run("brace tokens", func(t *testing.T) {
		got := NormalizeColors("{+Bw}Daily Journal{NK}")
		if got != "\x1b[1;37mDaily Journal\x1b[0m" {
			t.Fatalf("got %q", got)
		}
	})
}
