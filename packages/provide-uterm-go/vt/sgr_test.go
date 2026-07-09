//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vt

import "testing"

func attrsAfter(t *testing.T, seq string) Char {
	t.Helper()
	return feedNew(t, seq).Cursor().Attrs
}

func TestSGRBasicColorsAndStyles(t *testing.T) {
	a := attrsAfter(t, "\x1b[1;3;4;5;7;9;31;42m")
	allSet := a.Bold && a.Italics && a.Underscore && a.Blink && a.Reverse &&
		a.Strikethrough && a.FG == "red" && a.BG == "green"
	if !allSet {
		t.Errorf("attrs = %+v", a)
	}
	if a.Dim {
		t.Error("Dim must never be set (pyte has no dim attribute)")
	}

	a = attrsAfter(t, "\x1b[1;3;4;5;7;9m\x1b[22;23;24;25;27;29m")
	if a.Bold || a.Italics || a.Underscore || a.Blink || a.Reverse || a.Strikethrough {
		t.Errorf("style resets: %+v", a)
	}
}

func TestSGRResetVariants(t *testing.T) {
	// CSI m and CSI 0m both reset everything.
	a := attrsAfter(t, "\x1b[1;31m\x1b[m")
	if a != defaultCharPlain {
		t.Errorf("CSI m reset: %+v", a)
	}
	// A 0 in a longer list resets, later codes then apply.
	a = attrsAfter(t, "\x1b[1;31m\x1b[0;44m")
	if a.Bold || a.FG != "default" || a.BG != "blue" {
		t.Errorf("0-then-44: %+v", a)
	}
	// Direct API: no args resets too.
	s := NewScreen(80, 24)
	s.SelectGraphicRendition(1, 31)
	s.SelectGraphicRendition()
	if got := s.Cursor().Attrs; got != defaultCharPlain {
		t.Errorf("no-arg SGR: %+v", got)
	}
}

func TestSGRUnknownCodesIgnored(t *testing.T) {
	a := attrsAfter(t, "\x1b[2;6;8;21;51m")
	if a != defaultCharPlain {
		t.Errorf("unknown codes must be ignored: %+v", a)
	}
}

func TestSGRAixterm(t *testing.T) {
	a := attrsAfter(t, "\x1b[91;103m")
	if a.FG != "brightred" || a.BG != "brightbrown" {
		t.Errorf("aixterm: %+v", a)
	}
	// pyte's BG_AIXTERM has a typo for 105; parity requires reproducing it.
	a = attrsAfter(t, "\x1b[105m")
	if a.BG != "bfightmagenta" {
		t.Errorf("bg 105 = %q, want pyte's bfightmagenta", a.BG)
	}
}

func TestSGR256Colors(t *testing.T) {
	if got := attrsAfter(t, "\x1b[38;5;196m").FG; got != "ff0000" {
		t.Errorf("fg 196 = %q", got)
	}
	if got := attrsAfter(t, "\x1b[48;5;21m").BG; got != "0000ff" {
		t.Errorf("bg 21 = %q", got)
	}
	if got := attrsAfter(t, "\x1b[38;5;232m").FG; got != "080808" {
		t.Errorf("fg 232 = %q", got)
	}
	if got := attrsAfter(t, "\x1b[38;5;7m").FG; got != "e5e5e5" {
		t.Errorf("fg 7 = %q", got)
	}
	// Out-of-range index: consumed, ignored, later params still apply.
	a := attrsAfter(t, "\x1b[38;5;300;31m")
	if a.FG != "red" {
		t.Errorf("oob palette then 31: %+v", a)
	}
	// Truncated forms are swallowed.
	if got := attrsAfter(t, "\x1b[38;5m"); got != defaultCharPlain {
		t.Errorf("truncated 38;5: %+v", got)
	}
	if got := attrsAfter(t, "\x1b[38m"); got != defaultCharPlain {
		t.Errorf("bare 38: %+v", got)
	}
	// Unknown extended mode consumes its introducer parameter.
	a = attrsAfter(t, "\x1b[38;7;31m")
	if a.FG != "red" {
		t.Errorf("38;7;31: %+v", a)
	}
}

func TestSGRTruecolor(t *testing.T) {
	if got := attrsAfter(t, "\x1b[38;2;255;128;0m").FG; got != "ff8000" {
		t.Errorf("truecolor fg = %q", got)
	}
	if got := attrsAfter(t, "\x1b[48;2;1;2;3m").BG; got != "010203" {
		t.Errorf("truecolor bg = %q", got)
	}
	// Components above 255 format to wider hex, like pyte.
	if got := attrsAfter(t, "\x1b[48;2;300;1;2m").BG; got != "12c0102" {
		t.Errorf("oob truecolor bg = %q", got)
	}
	// Truncated truecolor consumes the rest without effect.
	if got := attrsAfter(t, "\x1b[38;2;10;20m"); got != defaultCharPlain {
		t.Errorf("truncated truecolor: %+v", got)
	}
}

func TestSGRMixedExtendedRun(t *testing.T) {
	a := attrsAfter(t, "\x1b[1;38;5;10;48;2;9;9;9;4m")
	ok := a.Bold && a.Underscore && a.FG == "00ff00" && a.BG == "090909"
	if !ok {
		t.Errorf("mixed run: %+v", a)
	}
}

func TestSGRUnderDECSCNM(t *testing.T) {
	// Reset under DECSCNM keeps the reverse default.
	s := feedNew(t, "\x1b[?5h\x1b[0m")
	if !s.Cursor().Attrs.Reverse {
		t.Error("SGR 0 under DECSCNM must keep reverse")
	}
}
