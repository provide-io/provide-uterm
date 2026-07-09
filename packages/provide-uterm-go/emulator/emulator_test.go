//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package emulator

import (
	"encoding/hex"
	"encoding/json"
	"os"
	"strings"
	"testing"
)

// pythonGolden mirrors the fields dumped by the Python TerminalEmulator for
// the same byte streams (regenerate with the script noted in the repo's Go
// port docs; the raw bytes are embedded as hex).
type pythonGolden struct {
	RawHex  string         `json:"raw_hex"`
	Screen  string         `json:"screen"`
	Hash    string         `json:"hash"`
	Cursor  map[string]int `json:"cursor"`
	CAE     bool           `json:"cae"`
	HTS     bool           `json:"hts"`
	RawTail string         `json:"raw_tail"`
	ANSI0   string         `json:"ansi0"`
}

func TestGoldenParityWithPython(t *testing.T) {
	raw, err := os.ReadFile("testdata/python_golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []pythonGolden
	if err := json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	if len(cases) == 0 {
		t.Fatal("empty golden file")
	}
	for i, c := range cases {
		data, err := hex.DecodeString(c.RawHex)
		if err != nil {
			t.Fatal(err)
		}
		e := New(40, 6, "")
		e.Process(data)
		snap := e.GetSnapshot()
		if snap.Screen != c.Screen {
			t.Fatalf("case %d screen:\n%q\nwant\n%q", i, snap.Screen, c.Screen)
		}
		if snap.ScreenHash != c.Hash {
			t.Fatalf("case %d hash %s want %s", i, snap.ScreenHash, c.Hash)
		}
		if snap.Cursor.X != c.Cursor["x"] || snap.Cursor.Y != c.Cursor["y"] {
			t.Fatalf("case %d cursor %+v want %v", i, snap.Cursor, c.Cursor)
		}
		if snap.CursorAtEnd != c.CAE || snap.HasTrailingSpace != c.HTS {
			t.Fatalf("case %d cae=%v hts=%v want %v/%v", i, snap.CursorAtEnd, snap.HasTrailingSpace, c.CAE, c.HTS)
		}
		if snap.RawTail != c.RawTail {
			t.Fatalf("case %d raw_tail %q want %q", i, snap.RawTail, c.RawTail)
		}
		if got := strings.SplitN(e.ANSIScreen(), "\n", 2)[0]; got != c.ANSI0 {
			t.Fatalf("case %d ansi row0 %q want %q", i, got, c.ANSI0)
		}
	}
}

func TestDefaultsAndAccessors(t *testing.T) {
	e := New(0, 0, "")
	if e.Cols() != 80 || e.Rows() != 25 {
		t.Fatalf("defaults %dx%d", e.Cols(), e.Rows())
	}
	snap := e.GetSnapshot()
	if snap.Term != "ANSI" || snap.Cols != 80 || snap.Rows != 25 {
		t.Fatalf("snap = %+v", snap)
	}
	if e.Screen() == nil {
		t.Fatal("nil screen")
	}
	if snap.CapturedAt <= 0 {
		t.Fatal("captured_at not stamped")
	}
}

func TestSnapshotCachingAndDirtying(t *testing.T) {
	e := New(20, 4, "vt100")
	s1 := e.GetSnapshot()
	s2 := e.GetSnapshot()
	if s1.ScreenHash != s2.ScreenHash {
		t.Fatal("cache miss without writes")
	}
	e.Process([]byte("hi"))
	s3 := e.GetSnapshot()
	if s3.ScreenHash == s1.ScreenHash {
		t.Fatal("snapshot not refreshed after Process")
	}
	if !strings.HasPrefix(s3.Screen, "hi") {
		t.Fatalf("screen = %q", s3.Screen)
	}
}

func TestRawTailBounded(t *testing.T) {
	e := New(20, 4, "")
	chunk := strings.Repeat("x", 1500)
	for range 4 {
		e.Process([]byte(chunk))
	}
	if len(e.RawTail()) != rawTailMax {
		t.Fatalf("tail len = %d", len(e.RawTail()))
	}
	// Multi-byte CP437 output must not be split mid-rune by the trim.
	e2 := New(20, 4, "")
	e2.Process([]byte(strings.Repeat("y", rawTailMax-1)))
	e2.Process([]byte{0xC9, 0xC9}) // ╔╔ — 3 UTF-8 bytes each
	tail := e2.RawTail()
	if len(tail) > rawTailMax || !strings.HasSuffix(tail, "╔╔") {
		t.Fatalf("tail len=%d suffix=%q", len(tail), tail[len(tail)-8:])
	}
	// Empty writes leave the tail untouched.
	before := e2.RawTail()
	e2.Process(nil)
	if e2.RawTail() != before {
		t.Fatal("empty process changed tail")
	}
	// A tail made entirely of multi-byte runes forces the trim to walk
	// forward off a continuation byte.
	e3 := New(20, 4, "")
	e3.Process(bytesRepeat(0xC9, 2000)) // ╔ ×2000 → 6000 UTF-8 bytes
	tail3 := e3.RawTail()
	if len(tail3) > rawTailMax || len(tail3)%3 != 0 || !strings.HasPrefix(tail3, "╔") {
		t.Fatalf("tail3 len=%d", len(tail3))
	}
}

func bytesRepeat(b byte, n int) []byte {
	out := make([]byte, n)
	for i := range out {
		out[i] = b
	}
	return out
}

func TestCursorAtEndHeuristic(t *testing.T) {
	// Cursor above the last content line → not at end.
	e := New(20, 4, "")
	e.Process([]byte("line1\r\nline2\x1b[1;1H"))
	if e.GetSnapshot().CursorAtEnd {
		t.Fatal("cursor moved home must not be at end")
	}
	// Cursor below the last content line → at end.
	e2 := New(20, 4, "")
	e2.Process([]byte("line1\r\n"))
	if !e2.GetSnapshot().CursorAtEnd {
		t.Fatal("cursor on the row after content is at end")
	}
	// Blank screen → at end.
	e3 := New(20, 4, "")
	if !e3.GetSnapshot().CursorAtEnd {
		t.Fatal("blank screen is at end")
	}
}

func TestResetAndResize(t *testing.T) {
	e := New(20, 4, "")
	e.Process([]byte("content"))
	e.Reset()
	if got := strings.TrimSpace(e.GetSnapshot().Screen); got != "" {
		t.Fatalf("screen after reset = %q", got)
	}
	e.Resize(10, 2)
	snap := e.GetSnapshot()
	if snap.Cols != 10 || snap.Rows != 2 {
		t.Fatalf("snap = %+v", snap)
	}
	if lines := strings.Split(snap.Screen, "\n"); len(lines) != 2 || len(lines[0]) != 10 {
		t.Fatalf("display = %q", snap.Screen)
	}
}
