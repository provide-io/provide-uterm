//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"reflect"
	"strings"
	"testing"
)

func TestLineBufferPrintableChars(t *testing.T) {
	b := NewLineBuffer()
	b.Feed("hi")
	if got := b.TakeEcho(); got != "hi" {
		t.Fatalf("echo = %q, want %q", got, "hi")
	}
	if got := b.TakeCompleted(); len(got) != 0 {
		t.Fatalf("completed = %v, want empty", got)
	}
}

func TestLineBufferTabPrintable(t *testing.T) {
	b := NewLineBuffer()
	b.Feed("\t")
	if got := b.TakeEcho(); got != "\t" {
		t.Fatalf("echo = %q", got)
	}
	if got := b.CurrentLine(); got != "\t" {
		t.Fatalf("current = %q", got)
	}
}

func TestLineBufferMultibyteRune(t *testing.T) {
	b := NewLineBuffer()
	b.Feed("a❯b")
	if got := b.CurrentLine(); got != "a❯b" {
		t.Fatalf("current = %q", got)
	}
	if got := b.TakeEcho(); got != "a❯b" {
		t.Fatalf("echo = %q", got)
	}
}

func TestLineBufferSubmit(t *testing.T) {
	tests := []struct {
		name      string
		feed      string
		wantLines []string
		echoHas   string
		echoCRLF  int
	}{
		{"cr", "hello\r", []string{"hello"}, "\r\n", 1},
		{"crlf", "abc\r\n", []string{"abc"}, "\r\n", 1},
		{"lf", "line\n", []string{"line"}, "\r\n", 1},
		{"just_enter", "\r", []string{""}, "\r\n", 1},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			b := NewLineBuffer()
			b.Feed(tt.feed)
			echo := b.TakeEcho()
			if !strings.Contains(echo, tt.echoHas) {
				t.Fatalf("echo %q missing %q", echo, tt.echoHas)
			}
			if n := strings.Count(echo, "\r\n"); n != tt.echoCRLF {
				t.Fatalf("crlf count = %d, want %d", n, tt.echoCRLF)
			}
			if got := b.TakeCompleted(); !reflect.DeepEqual(got, tt.wantLines) {
				t.Fatalf("completed = %v, want %v", got, tt.wantLines)
			}
		})
	}
}

func TestLineBufferBackspace(t *testing.T) {
	for _, bs := range []string{"\x7f", "\x08"} {
		b := NewLineBuffer()
		b.Feed("ab" + bs)
		if got := b.CurrentLine(); got != "a" {
			t.Fatalf("current = %q", got)
		}
		if echo := b.TakeEcho(); !strings.Contains(echo, "\x08 \x08") {
			t.Fatalf("echo %q missing backspace seq", echo)
		}
	}
}

func TestLineBufferBackspaceOnEmpty(t *testing.T) {
	b := NewLineBuffer()
	b.Feed("\x7f")
	if got := b.TakeEcho(); got != "" {
		t.Fatalf("echo = %q, want empty", got)
	}
	if got := b.CurrentLine(); got != "" {
		t.Fatalf("current = %q", got)
	}
}

func TestLineBufferCtrlC(t *testing.T) {
	b := NewLineBuffer()
	b.Feed("partial\x03")
	if got := b.TakeCompleted(); !reflect.DeepEqual(got, []string{"\x03"}) {
		t.Fatalf("completed = %v", got)
	}
	if got := b.CurrentLine(); got != "" {
		t.Fatalf("current = %q", got)
	}
	if echo := b.TakeEcho(); !strings.Contains(echo, "^C") {
		t.Fatalf("echo %q missing ^C", echo)
	}
}

func TestLineBufferCtrlD(t *testing.T) {
	tests := []struct {
		name string
		feed string
		want []string
	}{
		{"with_content", "hello\x04", []string{"hello"}},
		{"empty_buffer", "\x04", []string{"\x04"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			b := NewLineBuffer()
			b.Feed(tt.feed)
			if got := b.TakeCompleted(); !reflect.DeepEqual(got, tt.want) {
				t.Fatalf("completed = %v, want %v", got, tt.want)
			}
			if echo := b.TakeEcho(); !strings.Contains(echo, "\r\n") {
				t.Fatalf("echo %q missing crlf", echo)
			}
		})
	}
}

func TestLineBufferEscapeSequences(t *testing.T) {
	tests := []struct {
		name        string
		feed        string
		wantCurrent string
	}{
		{"csi_arrow", "\x1b[A", ""},
		{"csi_params", "\x1b[1;2A", ""},
		{"ss3", "\x1bOA", ""},
		{"esc_only", "\x1b", ""},
		{"esc_bracket_only", "\x1b[", ""},
		{"esc_o_only", "\x1bO", ""},
		{"csi_no_final_with_params", "\x1b[12", ""},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			b := NewLineBuffer()
			b.Feed(tt.feed)
			if got := b.TakeEcho(); got != "" {
				t.Fatalf("echo = %q, want empty", got)
			}
			if got := b.CurrentLine(); got != tt.wantCurrent {
				t.Fatalf("current = %q, want %q", got, tt.wantCurrent)
			}
		})
	}
}

func TestLineBufferEscapeThenPrintable(t *testing.T) {
	// An escape sequence followed by a printable char: the char survives.
	b := NewLineBuffer()
	b.Feed("\x1b[Ax")
	if got := b.CurrentLine(); got != "x" {
		t.Fatalf("current = %q, want %q", got, "x")
	}
}

func TestLineBufferOtherControlIgnored(t *testing.T) {
	b := NewLineBuffer()
	b.Feed("\x01")
	if got := b.TakeEcho(); got != "" {
		t.Fatalf("echo = %q", got)
	}
	if got := b.CurrentLine(); got != "" {
		t.Fatalf("current = %q", got)
	}
}

func TestLineBufferMaxLine(t *testing.T) {
	b := NewLineBufferMax(3)
	b.Feed("abcde")
	if got := b.CurrentLine(); got != "abc" {
		t.Fatalf("current = %q, want abc", got)
	}
	if got := b.TakeEcho(); got != "abc" {
		t.Fatalf("echo = %q, want abc", got)
	}
}

func TestLineBufferTakeDrains(t *testing.T) {
	b := NewLineBuffer()
	b.Feed("hi")
	_ = b.TakeEcho()
	if got := b.TakeEcho(); got != "" {
		t.Fatalf("echo not drained: %q", got)
	}
	b.Feed("cmd\r")
	_ = b.TakeCompleted()
	if got := b.TakeCompleted(); len(got) != 0 {
		t.Fatalf("completed not drained: %v", got)
	}
}

func TestLineBufferClear(t *testing.T) {
	b := NewLineBuffer()
	b.Feed("something")
	b.Clear()
	if got := b.CurrentLine(); got != "" {
		t.Fatalf("current = %q", got)
	}
	if got := b.TakeEcho(); got != "" {
		t.Fatalf("echo = %q", got)
	}
}
