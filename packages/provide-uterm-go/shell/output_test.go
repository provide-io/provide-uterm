//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"strings"
	"testing"
)

func TestErrorMsg(t *testing.T) {
	msg := ErrorMsg("bad thing")
	if !strings.Contains(msg, "bad thing") || !strings.Contains(msg, Red) || !strings.Contains(msg, Reset) {
		t.Fatalf("error msg = %q", msg)
	}
	if !strings.HasSuffix(msg, "\r\n") {
		t.Fatalf("error msg not crlf-terminated: %q", msg)
	}
}

func TestInfoSuccessHeading(t *testing.T) {
	if m := InfoMsg("some info"); !strings.Contains(m, "some info") || !strings.Contains(m, Dim) || !strings.HasSuffix(m, "\r\n") {
		t.Fatalf("info = %q", m)
	}
	if m := SuccessMsg("done"); !strings.Contains(m, "done") || !strings.Contains(m, Green) || !strings.HasSuffix(m, "\r\n") {
		t.Fatalf("success = %q", m)
	}
	if m := Heading("My Title"); !strings.Contains(m, "My Title") || !strings.Contains(m, Bold) || !strings.Contains(m, Cyan) || !strings.HasSuffix(m, "\r\n") {
		t.Fatalf("heading = %q", m)
	}
}

func TestFmtKV(t *testing.T) {
	m := FmtKVDefault("key", "val")
	if !strings.Contains(m, "key") || !strings.Contains(m, "val") || !strings.HasSuffix(m, "\r\n") {
		t.Fatalf("kv = %q", m)
	}
	// Default width pads the key to 20 columns.
	if !strings.Contains(m, "key"+strings.Repeat(" ", 17)) {
		t.Fatalf("kv not padded to 20: %q", m)
	}
	// Custom width.
	m2 := FmtKV("k", "v", 5)
	if !strings.Contains(m2, "k"+strings.Repeat(" ", 4)) {
		t.Fatalf("kv custom width = %q", m2)
	}
}

func TestFmtKVKeyWiderThanWidth(t *testing.T) {
	m := FmtKV("longkey", "v", 3)
	// Key wider than the width is emitted unpadded, followed by Reset then value.
	if !strings.Contains(m, Dim+"longkey"+Reset+"v") {
		t.Fatalf("kv wide key = %q", m)
	}
}

func TestFmtTableEmpty(t *testing.T) {
	if r := FmtTable(nil, nil); !strings.Contains(r, "(no results)") {
		t.Fatalf("empty table = %q", r)
	}
}

func TestFmtTableNoHeaders(t *testing.T) {
	r := FmtTable([][]string{{"a", "b"}, {"cc", "dd"}}, nil)
	for _, want := range []string{"a", "cc", "\r\n"} {
		if !strings.Contains(r, want) {
			t.Fatalf("table %q missing %q", r, want)
		}
	}
}

func TestFmtTableWithHeaders(t *testing.T) {
	r := FmtTable([][]string{{"alice", "admin"}, {"bob", "viewer"}}, []string{"name", "role"})
	for _, want := range []string{"name", "role", "alice", "bob", "-"} {
		if !strings.Contains(r, want) {
			t.Fatalf("table %q missing %q", r, want)
		}
	}
}

func TestFmtTableHeadersWiderThanData(t *testing.T) {
	r := FmtTable([][]string{{"a", "b"}}, []string{"longerheader", "anotherlong"})
	if !strings.Contains(r, "longerheader") {
		t.Fatalf("table = %q", r)
	}
}

func TestConstantsDefined(t *testing.T) {
	for _, c := range []string{Reset, Bold, Dim, Green, Yellow, Red, Cyan, Blue, Magenta, ClearScreen, Banner, Prompt} {
		if c == "" {
			t.Fatal("empty constant")
		}
	}
}
