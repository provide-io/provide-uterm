//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package lineeditor

import (
	"errors"
	"strings"
	"testing"
)

type recorder struct {
	out strings.Builder
	err error
}

func (r *recorder) write(s string) error {
	if r.err != nil {
		return r.err
	}
	r.out.WriteString(s)
	return nil
}

func typeString(t *testing.T, e *LineEditor, s string) (string, bool) {
	t.Helper()
	for _, ch := range s {
		line, done, err := e.ProcessChar(ch)
		if err != nil {
			t.Fatal(err)
		}
		if done {
			return line, true
		}
	}
	return "", false
}

func TestTypeAndEnter(t *testing.T) {
	rec := &recorder{}
	e := New(80, false, rec.write)
	line, done := typeString(t, e, "hello\r")
	if !done || line != "hello" {
		t.Fatalf("line=%q done=%v", line, done)
	}
	if rec.out.String() != "hello\r\n" {
		t.Fatalf("out = %q", rec.out.String())
	}
	if e.Buffer() != "" {
		t.Fatalf("buffer = %q", e.Buffer())
	}
}

func TestNewlineAlsoCompletes(t *testing.T) {
	e := New(0, false, nil) // default max length, silent mode
	line, done := typeString(t, e, "ok\n")
	if !done || line != "ok" {
		t.Fatalf("line=%q done=%v", line, done)
	}
	if e.MaxLength != 80 {
		t.Fatalf("MaxLength = %d", e.MaxLength)
	}
}

func TestBackspace(t *testing.T) {
	rec := &recorder{}
	e := New(80, false, rec.write)
	typeString(t, e, "abc")
	rec.out.Reset()
	if _, _, err := e.ProcessChar(0x7f); err != nil {
		t.Fatal(err)
	}
	if e.Buffer() != "ab" {
		t.Fatalf("buffer = %q", e.Buffer())
	}
	// At end of line: tail empty → BS, redraw "", space, move left 1.
	if rec.out.String() != "\x08 \x1b[1D" {
		t.Fatalf("out = %q", rec.out.String())
	}
	// Backspace at column 0 is a no-op.
	e2 := New(80, false, rec.write)
	rec.out.Reset()
	if _, _, err := e2.ProcessChar(0x08); err != nil {
		t.Fatal(err)
	}
	if rec.out.Len() != 0 {
		t.Fatalf("out = %q", rec.out.String())
	}
}

func TestBackspaceMovesCursor(t *testing.T) {
	// The backspace handler must decrement cursorPos; a following insert then
	// lands at the correct position. With the decrement broken the cursor would
	// point past the (now shorter) buffer and the insert would panic.
	e := New(80, false, nil)
	typeString(t, e, "abc") // buffer "abc", cursor 3
	if _, _, err := e.ProcessChar(0x7f); err != nil {
		t.Fatal(err)
	}
	typeString(t, e, "z") // insert at cursor 2 -> "abz"
	if e.Buffer() != "abz" {
		t.Fatalf("buffer = %q", e.Buffer())
	}
}

func TestBackspaceMidLine(t *testing.T) {
	rec := &recorder{}
	e := New(80, false, rec.write)
	typeString(t, e, "abcd")
	// Move to after 'b' (Ctrl+A then two Ctrl+F).
	typeString(t, e, "\x01\x06\x06")
	rec.out.Reset()
	if _, _, err := e.ProcessChar(0x7f); err != nil {
		t.Fatal(err)
	}
	if e.Buffer() != "acd" {
		t.Fatalf("buffer = %q", e.Buffer())
	}
	if rec.out.String() != "\x08cd \x1b[3D" {
		t.Fatalf("out = %q", rec.out.String())
	}
}

func TestCtrlAEHome(t *testing.T) {
	rec := &recorder{}
	e := New(80, false, rec.write)
	typeString(t, e, "abc")
	rec.out.Reset()
	typeString(t, e, "\x01") // Ctrl+A
	if rec.out.String() != "\x1b[3D" {
		t.Fatalf("out = %q", rec.out.String())
	}
	rec.out.Reset()
	typeString(t, e, "\x01") // already at start: no-op
	if rec.out.Len() != 0 {
		t.Fatalf("out = %q", rec.out.String())
	}
	rec.out.Reset()
	typeString(t, e, "\x05") // Ctrl+E
	if rec.out.String() != "\x1b[3C" {
		t.Fatalf("out = %q", rec.out.String())
	}
	rec.out.Reset()
	typeString(t, e, "\x05") // already at end: no-op
	if rec.out.Len() != 0 {
		t.Fatalf("out = %q", rec.out.String())
	}
}

func TestCtrlBFMovement(t *testing.T) {
	rec := &recorder{}
	e := New(80, false, rec.write)
	typeString(t, e, "ab")
	rec.out.Reset()
	typeString(t, e, "\x02") // left
	if rec.out.String() != "\x1b[D" {
		t.Fatalf("out = %q", rec.out.String())
	}
	rec.out.Reset()
	typeString(t, e, "\x06") // right
	if rec.out.String() != "\x1b[C" {
		t.Fatalf("out = %q", rec.out.String())
	}
	rec.out.Reset()
	typeString(t, e, "\x06") // at end: no-op
	if rec.out.Len() != 0 {
		t.Fatalf("out = %q", rec.out.String())
	}
	e2 := New(80, false, rec.write)
	rec.out.Reset()
	typeString(t, e2, "\x02") // at start: no-op
	if rec.out.Len() != 0 {
		t.Fatalf("out = %q", rec.out.String())
	}
}

func TestCtrlUKillBackward(t *testing.T) {
	rec := &recorder{}
	e := New(80, false, rec.write)
	typeString(t, e, "abcd\x02\x02") // cursor after "ab"
	rec.out.Reset()
	typeString(t, e, "\x15")
	if e.Buffer() != "cd" {
		t.Fatalf("buffer = %q", e.Buffer())
	}
	if rec.out.String() != "\x1b[2Dcd\x1b[K\x1b[2D" {
		t.Fatalf("out = %q", rec.out.String())
	}
	// Kill everything (cursor at end): remaining empty, no trailing move.
	e2 := New(80, false, rec.write)
	typeString(t, e2, "xy")
	rec.out.Reset()
	typeString(t, e2, "\x15")
	if e2.Buffer() != "" || rec.out.String() != "\x1b[2D\x1b[K" {
		t.Fatalf("buffer=%q out=%q", e2.Buffer(), rec.out.String())
	}
	// At start: no-op.
	rec.out.Reset()
	typeString(t, e2, "\x15")
	if rec.out.Len() != 0 {
		t.Fatalf("out = %q", rec.out.String())
	}
}

func TestCtrlKKillForward(t *testing.T) {
	rec := &recorder{}
	e := New(80, false, rec.write)
	typeString(t, e, "abcd\x01") // cursor at start
	rec.out.Reset()
	typeString(t, e, "\x0b")
	if e.Buffer() != "" || rec.out.String() != "\x1b[K" {
		t.Fatalf("buffer=%q out=%q", e.Buffer(), rec.out.String())
	}
	// At end: no-op.
	rec.out.Reset()
	typeString(t, e, "\x0b")
	if rec.out.Len() != 0 {
		t.Fatalf("out = %q", rec.out.String())
	}
}

func TestCtrlWKillWordBackward(t *testing.T) {
	rec := &recorder{}
	e := New(80, false, rec.write)
	typeString(t, e, "foo bar  ")
	rec.out.Reset()
	typeString(t, e, "\x17")
	if e.Buffer() != "foo " {
		t.Fatalf("buffer = %q", e.Buffer())
	}
	// deleted = "bar  " (5 chars), remaining empty.
	if rec.out.String() != "\x1b[5D\x1b[K" {
		t.Fatalf("out = %q", rec.out.String())
	}
	// Mid-line: kill word before cursor, redraw remaining.
	e2 := New(80, false, rec.write)
	typeString(t, e2, "one two three")
	typeString(t, e2, strings.Repeat("\x02", 6)) // cursor after "one two"
	rec.out.Reset()
	typeString(t, e2, "\x17")
	if e2.Buffer() != "one  three" {
		t.Fatalf("buffer = %q", e2.Buffer())
	}
	if rec.out.String() != "\x1b[3D three\x1b[K\x1b[6D" {
		t.Fatalf("out = %q", rec.out.String())
	}
	// At start: no-op.
	e3 := New(80, false, rec.write)
	rec.out.Reset()
	typeString(t, e3, "\x17")
	if rec.out.Len() != 0 {
		t.Fatalf("out = %q", rec.out.String())
	}
}

func TestCtrlWReachesBufferStart(t *testing.T) {
	// A word extending to index 0 (no leading space): the second scan loop must
	// stop at pos==0 rather than read buffer[-1].
	e := New(80, false, nil)
	typeString(t, e, "foo")
	if _, _, err := e.ProcessChar(0x17); err != nil {
		t.Fatal(err)
	}
	if e.Buffer() != "" {
		t.Fatalf("word-at-start: buffer = %q", e.Buffer())
	}
	// An all-spaces buffer: the first (trailing-space) scan loop must stop at
	// pos==0 rather than read buffer[-1].
	e2 := New(80, false, nil)
	typeString(t, e2, "   ")
	if _, _, err := e2.ProcessChar(0x17); err != nil {
		t.Fatal(err)
	}
	if e2.Buffer() != "" {
		t.Fatalf("all-spaces: buffer = %q", e2.Buffer())
	}
}

func TestMaxLengthBeeps(t *testing.T) {
	rec := &recorder{}
	e := New(3, false, rec.write)
	typeString(t, e, "abc")
	rec.out.Reset()
	typeString(t, e, "d")
	if e.Buffer() != "abc" || rec.out.String() != "\a" {
		t.Fatalf("buffer=%q out=%q", e.Buffer(), rec.out.String())
	}
}

func TestMidLineInsert(t *testing.T) {
	rec := &recorder{}
	e := New(80, false, rec.write)
	typeString(t, e, "ac\x02") // cursor before 'c'
	rec.out.Reset()
	typeString(t, e, "b")
	if e.Buffer() != "abc" {
		t.Fatalf("buffer = %q", e.Buffer())
	}
	if rec.out.String() != "bc\x1b[1D" {
		t.Fatalf("out = %q", rec.out.String())
	}
}

func TestPasswordMode(t *testing.T) {
	rec := &recorder{}
	e := New(80, true, rec.write)
	typeString(t, e, "ab")
	if rec.out.String() != "**" {
		t.Fatalf("out = %q", rec.out.String())
	}
	// Mid-line insert masks the redraw too.
	typeString(t, e, "\x02")
	rec.out.Reset()
	typeString(t, e, "x")
	if e.Buffer() != "axb" || rec.out.String() != "**\x1b[1D" {
		t.Fatalf("buffer=%q out=%q", e.Buffer(), rec.out.String())
	}
	line, done := typeString(t, e, "\r")
	if !done || line != "axb" {
		t.Fatalf("line=%q", line)
	}
}

func TestSetters(t *testing.T) {
	e := New(80, false, nil)
	e.MaxLength = 2
	e.PasswordMode = true // pragma: allowlist secret
	typeString(t, e, "ab")
	if _, _, err := e.ProcessChar('c'); err != nil {
		t.Fatal(err)
	}
	if e.Buffer() != "ab" {
		t.Fatalf("buffer = %q", e.Buffer())
	}
	e.Reset()
	if e.Buffer() != "" {
		t.Fatalf("buffer = %q", e.Buffer())
	}
}

func TestWriteErrorsPropagate(t *testing.T) {
	rec := &recorder{err: errors.New("broken pipe")}
	e := New(80, false, rec.write)
	if _, _, err := e.ProcessChar('a'); err == nil {
		t.Fatal("expected error")
	}
	// Enter still returns the completed line alongside the emit error.
	rec2 := &recorder{}
	e2 := New(80, false, rec2.write)
	typeString(t, e2, "hi")
	rec2.err = errors.New("gone")
	line, done, err := e2.ProcessChar('\r')
	if err == nil || !done || line != "hi" {
		t.Fatalf("line=%q done=%v err=%v", line, done, err)
	}
}

func TestUnicodeRunes(t *testing.T) {
	rec := &recorder{}
	e := New(80, false, rec.write)
	typeString(t, e, "é→")
	if e.Buffer() != "é→" {
		t.Fatalf("buffer = %q", e.Buffer())
	}
	// Backspace removes one rune, not one byte.
	typeString(t, e, string(rune(0x7f)))
	if e.Buffer() != "é" {
		t.Fatalf("buffer = %q", e.Buffer())
	}
}
