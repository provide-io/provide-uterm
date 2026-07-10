//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package colors

import (
	"bytes"
	"testing"
)

// ── clamp8 ──────────────────────────────────────────────────────────────────

func TestClamp8(t *testing.T) {
	tests := []struct{ in, want int }{
		{-1, 0},
		{-1000, 0},
		{0, 0},
		{128, 128},
		{255, 255},
		{256, 255},
		{99999, 255},
	}
	for _, tt := range tests {
		if got := clamp8(tt.in); got != tt.want {
			t.Errorf("clamp8(%d) = %d, want %d", tt.in, got, tt.want)
		}
	}
}

// ── RGBTo256 ────────────────────────────────────────────────────────────────

func TestRGBTo256(t *testing.T) {
	tests := []struct {
		name    string
		r, g, b int
		want    int
	}{
		{"pure black returns 16", 0, 0, 0, 16},
		{"low grey returns 16", 7, 7, 7, 16},
		{"grey 8 uses ramp", 8, 8, 8, 232},
		{"grey 248 uses ramp", 248, 248, 248, 255},
		{"grey 249 returns 231", 249, 249, 249, 231},
		{"grey 255 returns 231", 255, 255, 255, 231},
		{"pure red cube", 255, 0, 0, 196},
		{"pure green cube", 0, 255, 0, 46},
		{"pure blue cube", 0, 0, 255, 21},
		{"clamp over-range", 300, 0, 0, 196},
		{"clamp negative to grey branch", -1, 0, 0, 16},
		{"midrange non-grey", 128, 64, 200, 134},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := RGBTo256(tt.r, tt.g, tt.b); got != tt.want {
				t.Errorf("RGBTo256(%d, %d, %d) = %d, want %d", tt.r, tt.g, tt.b, got, tt.want)
			}
		})
	}
}

func TestRGBTo256GreyRampMonotonic(t *testing.T) {
	prev := 232
	for v := 8; v <= 248; v++ {
		got := RGBTo256(v, v, v)
		if got < prev || got > 255 {
			t.Fatalf("RGBTo256(%d,%d,%d) = %d, not monotonic in [232,255]", v, v, v, got)
		}
		prev = got
	}
}

// ── RGBTo16Index ────────────────────────────────────────────────────────────

func TestRGBTo16IndexExactPaletteRoundTrip(t *testing.T) {
	for want, p := range palette16 {
		if got := RGBTo16Index(p[0], p[1], p[2]); got != want {
			t.Errorf("palette16[%d]=%v round-tripped to %d", want, p, got)
		}
	}
}

func TestRGBTo16Index(t *testing.T) {
	tests := []struct {
		name    string
		r, g, b int
		want    int
	}{
		{"bright white exact", 255, 255, 255, 15},
		{"bright red exact", 255, 92, 92, 12},
		{"near black", 1, 1, 1, 0},
		{"near red palette entry", 200, 5, 5, 4},
		{"clamp over-range to bright white", 400, 400, 400, 15},
		{"clamp negative to black", -100, -100, -100, 0},
		// Distance tie between palette idx 1 ({0,0,205}) and idx 9 ({92,92,255}):
		// both are exactly 9089 away. Strict `<` keeps the first (lower) index,
		// matching Python's rgb_to_16_index (first-wins). A `<=` regression would
		// return 9.
		{"distance tie prefers lower index", 0, 92, 230, 1},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := RGBTo16Index(tt.r, tt.g, tt.b); got != tt.want {
				t.Errorf("RGBTo16Index(%d, %d, %d) = %d, want %d", tt.r, tt.g, tt.b, got, tt.want)
			}
		})
	}
}

// ── isDigits / parseComponent ───────────────────────────────────────────────

func TestIsDigits(t *testing.T) {
	tests := []struct {
		in   string
		want bool
	}{
		{"", false},
		{"0", true},
		{"255", true},
		{"9999999999", true},
		{"x", false},
		{"12a", false},
		{"1 2", false},
	}
	for _, tt := range tests {
		if got := isDigits(tt.in); got != tt.want {
			t.Errorf("isDigits(%q) = %v, want %v", tt.in, got, tt.want)
		}
	}
}

func TestParseComponent(t *testing.T) {
	tests := []struct {
		in   string
		want int
	}{
		{"0", 0},
		{"010", 10},
		{"255", 255},
		{"000000000255", 255}, // long but zero-padded — real value kept
		// Saturation boundary: a 9-digit value is parsed as-is (fits int, and is
		// clamped to 255 downstream); saturation only kicks in above 9 digits.
		{"999999999", 999999999}, // exactly 9 digits — real value kept
		{"1000000000", 1 << 30},  // 10 digits — saturated to 1<<30
	}
	for _, tt := range tests {
		if got := parseComponent(tt.in); got != tt.want {
			t.Errorf("parseComponent(%q) = %d, want %d", tt.in, got, tt.want)
		}
	}
	if got := parseComponent("99999999999999999999"); got <= 255 {
		t.Errorf("parseComponent(huge) = %d, want > 255", got)
	}
}

// ── RewriteParams ───────────────────────────────────────────────────────────

func TestRewriteParams(t *testing.T) {
	tests := []struct {
		name   string
		params string
		mode   ColorMode
		want   string
	}{
		{"empty params 256", "", Mode256, "\x1b[m"},
		{"empty params 16", "", Mode16, "\x1b[m"},
		{"reset passthrough", "0", Mode256, "\x1b[0m"},
		{"bold passthrough", "1", Mode16, "\x1b[1m"},
		{"truecolor fg to 256", "38;2;255;0;0", Mode256, "\x1b[38;5;196m"},
		{"truecolor bg to 256", "48;2;0;255;0", Mode256, "\x1b[48;5;46m"},
		{"truecolor fg to 16", "38;2;255;92;92", Mode16, "\x1b[91m"},
		{"truecolor bg to 16", "48;2;92;92;255", Mode16, "\x1b[104m"},
		{"already 256 passthrough (256)", "38;5;42", Mode256, "\x1b[38;5;42m"},
		{"already 256 passthrough (16)", "38;5;42", Mode16, "\x1b[38;5;42m"},
		{"mixed params preserved", "1;38;2;255;0;0", Mode256, "\x1b[1;38;5;196m"},
		{"truncated run preserved", "38;2;255", Mode256, "\x1b[38;2;255m"},
		// Exactly four params (head at index 0) makes i+4 == n, so the
		// `i+4 < n` bounds guard must reject the run — reading parts[i+4]
		// would be out of range. Preserved verbatim.
		{"four-param run lacks blue", "38;2;1;2", Mode256, "\x1b[38;2;1;2m"},
		{"non-digit params preserved", "38;2;x;y;z", Mode256, "\x1b[38;2;x;y;zm"},
		{"empty component not truecolor", "38;2;;0;0", Mode256, "\x1b[38;2;;0;0m"},
		{"multiple truecolor runs", "1;38;2;255;0;0;48;2;0;255;0", Mode256, "\x1b[1;38;5;196;48;5;46m"},
		{"bg truecolor to 16 black", "48;2;0;0;0", Mode16, "\x1b[40m"},
		{"leading-zero components", "38;2;010;0;0", Mode16, "\x1b[30m"},
		{"huge component clamps", "38;2;9999999999;0;0", Mode256, "\x1b[38;5;196m"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := RewriteParams(tt.params, tt.mode); got != tt.want {
				t.Errorf("RewriteParams(%q, %q) = %q, want %q", tt.params, tt.mode, got, tt.want)
			}
		})
	}
}

// ── DowngradeTo256 / DowngradeTo16 ──────────────────────────────────────────

func TestDowngrade(t *testing.T) {
	t.Run("to 256 basic", func(t *testing.T) {
		src := "\x1b[38;2;255;0;0mhello\x1b[0m"
		if got := DowngradeTo256(src); got != "\x1b[38;5;196mhello\x1b[0m" {
			t.Fatalf("got %q", got)
		}
	})
	t.Run("to 16 basic", func(t *testing.T) {
		src := "\x1b[38;2;255;92;92mhello\x1b[0m"
		if got := DowngradeTo16(src); got != "\x1b[91mhello\x1b[0m" {
			t.Fatalf("got %q", got)
		}
	})
	t.Run("to 256 idempotent on already-256", func(t *testing.T) {
		already := "\x1b[38;5;42mhello\x1b[0m"
		if got := DowngradeTo256(already); got != already {
			t.Fatalf("got %q", got)
		}
	})
	t.Run("to 16 idempotent on already-16", func(t *testing.T) {
		already := "\x1b[31mhello\x1b[0m"
		if got := DowngradeTo16(already); got != already {
			t.Fatalf("got %q", got)
		}
	})
	t.Run("plain text untouched", func(t *testing.T) {
		if DowngradeTo256("no colors here") != "no colors here" {
			t.Fatal("DowngradeTo256 modified plain text")
		}
		if DowngradeTo16("no colors here") != "no colors here" {
			t.Fatal("DowngradeTo16 modified plain text")
		}
	})
	t.Run("multiple sequences", func(t *testing.T) {
		src := "\x1b[38;2;255;0;0ma\x1b[0mb\x1b[38;2;0;255;0mc\x1b[0m"
		want := "\x1b[38;5;196ma\x1b[0mb\x1b[38;5;46mc\x1b[0m"
		if got := DowngradeTo256(src); got != want {
			t.Fatalf("got %q, want %q", got, want)
		}
	})
	t.Run("empty SGR untouched", func(t *testing.T) {
		if got := DowngradeTo256("\x1b[m"); got != "\x1b[m" {
			t.Fatalf("got %q", got)
		}
	})
}

// ── ApplyColorMode / ApplyColorModeBytes ────────────────────────────────────

func TestApplyColorModeString(t *testing.T) {
	tests := []struct {
		name string
		in   string
		mode ColorMode
		want string
	}{
		{"passthrough", "\x1b[38;2;255;0;0mhello\x1b[0m", ModePassthrough, "\x1b[38;2;255;0;0mhello\x1b[0m"},
		{"256", "\x1b[38;2;255;0;0mhello\x1b[0m", Mode256, "\x1b[38;5;196mhello\x1b[0m"},
		{"16", "\x1b[38;2;255;92;92mhello\x1b[0m", Mode16, "\x1b[91mhello\x1b[0m"},
		{"empty 256", "", Mode256, ""},
		{"empty 16", "", Mode16, ""},
		{"empty passthrough", "", ModePassthrough, ""},
		{"plain 256", "plain", Mode256, "plain"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := ApplyColorMode(tt.in, tt.mode); got != tt.want {
				t.Errorf("ApplyColorMode(%q, %q) = %q, want %q", tt.in, tt.mode, got, tt.want)
			}
		})
	}
}

func TestApplyColorModeBytes(t *testing.T) {
	tests := []struct {
		name string
		in   []byte
		mode ColorMode
		want []byte
	}{
		{"passthrough", []byte("\x1b[38;2;255;0;0mhello\x1b[0m"), ModePassthrough, []byte("\x1b[38;2;255;0;0mhello\x1b[0m")},
		{"256 roundtrip", []byte("\x1b[38;2;255;0;0mhello\x1b[0m"), Mode256, []byte("\x1b[38;5;196mhello\x1b[0m")},
		{"16 roundtrip", []byte("\x1b[38;2;255;92;92mhello\x1b[0m"), Mode16, []byte("\x1b[91mhello\x1b[0m")},
		{"non-ascii bytes preserved", []byte("\xe2\x98\x85\x1b[38;2;255;0;0m!\x1b[0m"), Mode256, []byte("\xe2\x98\x85\x1b[38;5;196m!\x1b[0m")},
		{"empty 256", []byte{}, Mode256, []byte{}},
		{"empty 16", []byte{}, Mode16, []byte{}},
		{"empty passthrough", []byte{}, ModePassthrough, []byte{}},
		{"plain 16", []byte("plain"), Mode16, []byte("plain")},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := ApplyColorModeBytes(tt.in, tt.mode); !bytes.Equal(got, tt.want) {
				t.Errorf("ApplyColorModeBytes(%q, %q) = %q, want %q", tt.in, tt.mode, got, tt.want)
			}
		})
	}
}

// ── SGRRegexp sanity ────────────────────────────────────────────────────────

func TestSGRRegexp(t *testing.T) {
	t.Run("matches typical SGR", func(t *testing.T) {
		m := SGRRegexp.FindStringSubmatch("\x1b[38;2;255;0;0m")
		if m == nil || m[1] != "38;2;255;0;0" {
			t.Fatalf("submatch = %v", m)
		}
	})
	t.Run("matches empty params", func(t *testing.T) {
		m := SGRRegexp.FindStringSubmatch("\x1b[m")
		if m == nil || m[1] != "" {
			t.Fatalf("submatch = %v", m)
		}
	})
	t.Run("does not match non-SGR CSI", func(t *testing.T) {
		if SGRRegexp.MatchString("\x1b[2J") {
			t.Fatal("SGRRegexp matched \\x1b[2J")
		}
	})
}
