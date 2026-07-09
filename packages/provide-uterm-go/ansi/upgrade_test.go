//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package ansi

import (
	"strings"
	"testing"
)

func TestUpgradeTo256Exact(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want string
	}{
		{"fg red", "\x1b[31mtext\x1b[0m", "\x1b[38;5;160mtext\x1b[0m"},
		{"fg black code30", "\x1b[30m", "\x1b[38;5;0m"},
		{"fg white code37", "\x1b[37m", "\x1b[38;5;252m"},
		{"bright fg black code90", "\x1b[90m", "\x1b[38;5;244m"},
		{"bright fg white code97", "\x1b[97m", "\x1b[38;5;231m"},
		{"bg black code40", "\x1b[40m", "\x1b[48;5;0m"},
		{"bg white code47", "\x1b[47m", "\x1b[48;5;252m"},
		{"bright bg black code100", "\x1b[100m", "\x1b[48;5;244m"},
		{"bright bg white code107", "\x1b[107m", "\x1b[48;5;231m"},
		{"bright fg 90 range", "\x1b[91mtext", "\x1b[38;5;196mtext"},
		{"bright bg 100 range", "\x1b[101mtext", "\x1b[48;5;196mtext"},
		{"code29 not mapped", "\x1b[29m", "\x1b[29m"},
		{"code39 not mapped", "\x1b[39m", "\x1b[39m"},
		{"code89 not mapped", "\x1b[89m", "\x1b[89m"},
		{"code98 not mapped", "\x1b[98m", "\x1b[98m"},
		{"code99 not mapped", "\x1b[99m", "\x1b[99m"},
		{"code108 not mapped", "\x1b[108m", "\x1b[108m"},
		{"existing 256 skipped", "\x1b[38;5;100mtext", "\x1b[38;5;100mtext"},
		{"existing 48 skipped", "\x1b[48;5;100m", "\x1b[48;5;100m"},
		{"existing 48;2 skipped", "\x1b[48;2;10;20;30m", "\x1b[48;2;10;20;30m"},
		{"empty seq passthrough", "\x1b[m", "\x1b[m"},
		{"empty part skipped", "\x1b[;31m", "\x1b[38;5;160m"},
		{"all empty parts passthrough", "\x1b[;;m", "\x1b[;;m"},
		{"noncolor code passthrough", "\x1b[1m", "\x1b[1m"},
		{"bg color", "\x1b[41m", "\x1b[48;5;160m"},
		{"bold plus red fg", "\x1b[1;31m", "\x1b[1;38;5;160m"},
		{"bold plus bg green", "\x1b[1;42m", "\x1b[1;48;5;34m"},
		{"trailing empty part dropped", "\x1b[1;m", "\x1b[1m"},
		{"leading zeros normalized", "\x1b[0031m", "\x1b[38;5;160m"},
		{"huge value passthrough", "\x1b[99999999999999999999m", "\x1b[99999999999999999999m"},
		{"P token", "{P3}", "{F184}"},
		{"T token", "{T3}", "{B184}"},
		{"P0 token", "{P0}", "{F0}"},
		{"T0 token", "{T0}", "{B0}"},
		{"P8 token", "{P8}", "{F244}"},
		{"T8 token", "{T8}", "{B244}"},
		{"P15 token", "{P15}", "{F231}"},
		{"T15 token", "{T15}", "{B231}"},
		{"P16 wraps to 0", "{P16}", "{F0}"},
		{"T16 wraps to 0", "{T16}", "{B0}"},
		{"plain text untouched", "no colors", "no colors"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := UpgradeTo256(tt.in, nil); got != tt.want {
				t.Errorf("UpgradeTo256(%q) = %q, want %q", tt.in, got, tt.want)
			}
		})
	}
}

func TestUpgradeTo256ExplicitPalette(t *testing.T) {
	custom := make([]int, 16)
	for i := range custom {
		custom[i] = 10
	}
	if got := UpgradeTo256("\x1b[31mtext", custom); !strings.Contains(got, "38;5;10") {
		t.Fatalf("got %q", got)
	}
}

func TestUpgradeTo256NilPaletteMatchesDefault(t *testing.T) {
	if UpgradeTo256("\x1b[32mtext", nil) != UpgradeTo256("\x1b[32mtext", DefaultPalette) {
		t.Fatal("nil palette differs from DefaultPalette")
	}
}

func TestUpgradeToTruecolorExact(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want string
	}{
		{"fg red", "\x1b[31mtext", "\x1b[38;2;215;0;0mtext"},
		{"fg black code30", "\x1b[30m", "\x1b[38;2;0;0;0m"},
		{"fg white code37", "\x1b[37m", "\x1b[38;2;208;208;208m"},
		{"bright fg black code90", "\x1b[90m", "\x1b[38;2;128;128;128m"},
		{"bright fg white code97", "\x1b[97m", "\x1b[38;2;255;255;255m"},
		{"bg black code40", "\x1b[40m", "\x1b[48;2;0;0;0m"},
		{"bg white code47", "\x1b[47m", "\x1b[48;2;208;208;208m"},
		{"bright bg black code100", "\x1b[100m", "\x1b[48;2;128;128;128m"},
		{"bright bg white code107", "\x1b[107m", "\x1b[48;2;255;255;255m"},
		{"bg red", "\x1b[41mtext", "\x1b[48;2;215;0;0mtext"},
		{"existing tc skipped", "\x1b[38;2;100;200;50mtext", "\x1b[38;2;100;200;50mtext"},
		{"existing 48 skipped", "\x1b[48;5;100m", "\x1b[48;5;100m"},
		{"existing 48;2 skipped", "\x1b[48;2;10;20;30m", "\x1b[48;2;10;20;30m"},
		{"empty seq passthrough", "\x1b[m", "\x1b[m"},
		{"empty part skipped", "\x1b[;31m", "\x1b[38;2;215;0;0m"},
		{"all empty parts passthrough", "\x1b[;;m", "\x1b[;;m"},
		{"noncolor code passthrough", "\x1b[1m", "\x1b[1m"},
		{"bold plus red fg", "\x1b[1;31m", "\x1b[1;38;2;215;0;0m"},
		{"bold plus bg green", "\x1b[1;42m", "\x1b[1;48;2;0;175;0m"},
		{"huge value passthrough", "\x1b[99999999999999999999m", "\x1b[99999999999999999999m"},
		{"P token exact rgb", "{P3}", "\x1b[38;2;215;215;0m"},
		{"T token exact rgb", "{T3}", "\x1b[48;2;215;215;0m"},
		{"P0 token exact rgb", "{P0}", "\x1b[38;2;0;0;0m"},
		{"T8 token exact rgb", "{T8}", "\x1b[48;2;128;128;128m"},
		{"P16 wraps to 0", "{P16}", "\x1b[38;2;0;0;0m"},
		{"T16 wraps to 0", "{T16}", "\x1b[48;2;0;0;0m"},
		{"plain text untouched", "no colors", "no colors"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := UpgradeToTruecolor(tt.in, nil); got != tt.want {
				t.Errorf("UpgradeToTruecolor(%q) = %q, want %q", tt.in, got, tt.want)
			}
		})
	}
}

func TestUpgradeToTruecolorExplicitPalette(t *testing.T) {
	// Palette where color 1 (red, SGR 31) maps to index 196.
	// color256ToRGB(196) = (255, 0, 0).
	custom := make([]int, 16)
	custom[1] = 196
	if got := UpgradeToTruecolor("\x1b[31mtext", custom); !strings.Contains(got, "38;2;255;0;0") {
		t.Fatalf("got %q", got)
	}
}

func TestUpgradeToTruecolorNilPaletteMatchesDefault(t *testing.T) {
	if UpgradeToTruecolor("\x1b[32mtext", nil) != UpgradeToTruecolor("\x1b[32mtext", DefaultPalette) {
		t.Fatal("nil palette differs from DefaultPalette")
	}
}

func TestNormalizeDigits(t *testing.T) {
	tests := []struct{ in, want string }{
		{"000", "0"},
		{"0", "0"},
		{"0031", "31"},
		{"31", "31"},
		{"99999999999999999999", "99999999999999999999"},
	}
	for _, tt := range tests {
		if got := normalizeDigits(tt.in); got != tt.want {
			t.Errorf("normalizeDigits(%q) = %q, want %q", tt.in, got, tt.want)
		}
	}
}
