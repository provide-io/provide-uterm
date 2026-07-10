//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package gateway

import (
	"bytes"
	"testing"
)

// TestStripIACTruncatedAndUnknown exercises stripIAC's boundary branches: a
// lone trailing IAC, a truncated IAC WILL at the end, an unknown command byte,
// and an unterminated subnegotiation (skipSubneg returning n).
func TestStripIACTruncatedAndUnknown(t *testing.T) {
	if got := stripIAC([]byte{'a', iacIAC}); !bytes.Equal(got, []byte{'a'}) {
		t.Errorf("lone trailing IAC = %v, want %q", got, "a")
	}
	if got := stripIAC([]byte{'a', iacIAC, iacWILL}); !bytes.Equal(got, []byte{'a'}) {
		t.Errorf("truncated IAC WILL = %v, want %q", got, "a")
	}
	// Unknown command byte after IAC is skipped (2 bytes consumed).
	if got := stripIAC([]byte{iacIAC, 199, 'b'}); !bytes.Equal(got, []byte{'b'}) {
		t.Errorf("unknown command = %v, want %q", got, "b")
	}
	// IAC SB never closed by IAC SE: skipSubneg walks to the end and drops it.
	if got := stripIAC([]byte{iacIAC, iacSB, optTTYPE, 'x', 'y'}); len(got) != 0 {
		t.Errorf("unterminated subneg should be dropped, got %v", got)
	}
}

// TestFeedPendingBoundaries drives IacNegotiator.Feed through its split-frame
// "pending" branches: a lone trailing IAC, a truncated IAC SB (option byte not
// yet arrived), and a top-level IAC IAC (literal 0xff into cleaned data).
func TestFeedPendingBoundaries(t *testing.T) {
	// Lone trailing IAC is buffered until the rest of the command arrives.
	neg := NewIacNegotiator()
	if reply, cleaned := neg.Feed([]byte{iacIAC}); len(reply) != 0 || len(cleaned) != 0 {
		t.Fatalf("lone IAC should produce nothing yet, got reply=%v cleaned=%v", reply, cleaned)
	}
	reply, _ := neg.Feed([]byte{iacWILL, optTTYPE})
	if !bytes.Equal(reply, []byte{iacIAC, iacSB, optTTYPE, subSEND, iacIAC, iacSE}) {
		t.Fatalf("completed WILL TTYPE reply = %v", reply)
	}

	// Truncated IAC SB (option byte not present) is buffered, then completed.
	neg2 := NewIacNegotiator()
	if reply, cleaned := neg2.Feed([]byte{iacIAC, iacSB}); len(reply) != 0 || len(cleaned) != 0 {
		t.Fatalf("truncated IAC SB should buffer, got reply=%v cleaned=%v", reply, cleaned)
	}
	sb := append([]byte{optTTYPE, subIS}, []byte("XTERM")...)
	sb = append(sb, iacIAC, iacSE)
	neg2.Feed(sb)
	if neg2.Term != "xterm" {
		t.Errorf("term after resumed SB = %q", neg2.Term)
	}

	// Top-level IAC IAC is unescaped into the cleaned application data.
	neg3 := NewIacNegotiator()
	if _, cleaned := neg3.Feed([]byte{'a', iacIAC, iacIAC, 'b'}); !bytes.Equal(cleaned, []byte{'a', 0xff, 'b'}) {
		t.Errorf("top-level IAC IAC cleaned = %v", cleaned)
	}
}

// TestFeedSubnegEscapeAndDefault covers the IAC IAC escape while inside a
// subnegotiation, and the default (ignored) command branch for a bare IAC SE.
func TestFeedSubnegEscapeAndDefault(t *testing.T) {
	neg := NewIacNegotiator()
	// SB TTYPE IS <0xff (escaped as IAC IAC)> SE → payload holds one 0xff byte.
	data := []byte{iacIAC, iacSB, optTTYPE, subIS, iacIAC, iacIAC, iacIAC, iacSE}
	neg.Feed(data)
	if !neg.ttypeReceived {
		t.Error("escaped subneg should still finish the TTYPE option")
	}

	// A top-level IAC followed by an unhandled command (bare SE) is skipped.
	neg2 := NewIacNegotiator()
	if reply, cleaned := neg2.Feed([]byte{iacIAC, iacSE, 'z'}); len(reply) != 0 || !bytes.Equal(cleaned, []byte{'z'}) {
		t.Errorf("bare IAC SE default = reply %v cleaned %v", reply, cleaned)
	}
}

// TestHandleOptionUnhandledVerb covers the handleOption fall-through (a verb
// that is not WILL TTYPE / WILL NEW-ENVIRON produces no reply).
func TestHandleOptionUnhandledVerb(t *testing.T) {
	neg := NewIacNegotiator()
	if reply, _ := neg.Feed([]byte{iacIAC, iacWONT, optTTYPE}); len(reply) != 0 {
		t.Errorf("WONT TTYPE should produce no reply, got %v", reply)
	}
	// WILL NEW-ENVIRON exercises the second matched branch of handleOption.
	if reply, _ := neg.Feed([]byte{iacIAC, iacWILL, optNewEnviron}); !bytes.Equal(
		reply, []byte{iacIAC, iacSB, optNewEnviron, subSEND, iacIAC, iacSE}) {
		t.Errorf("WILL NEW-ENVIRON reply = %v", reply)
	}
}

// TestParseTTypeISNonIS covers the guard that rejects a TTYPE payload whose
// first byte is not the IS marker (or an empty payload).
func TestParseTTypeISNonIS(t *testing.T) {
	if got := parseTTypeIS(nil); got != "" {
		t.Errorf("empty payload = %q", got)
	}
	if got := parseTTypeIS([]byte{subSEND, 'x'}); got != "" {
		t.Errorf("non-IS payload = %q", got)
	}
}

// TestParseNewEnvironBranches exercises parseNewEnvironIS boundary handling:
// non-IS payload, an invalid marker byte, escaped bytes in both the name and
// value, and an empty-name entry (skipped).
func TestParseNewEnvironBranches(t *testing.T) {
	if got := parseNewEnvironIS(nil); len(got) != 0 {
		t.Errorf("empty payload = %v", got)
	}
	if got := parseNewEnvironIS([]byte{subSEND}); len(got) != 0 {
		t.Errorf("non-IS payload = %v", got)
	}
	// After IS, a byte that is neither VAR nor USERVAR aborts the parse.
	if got := parseNewEnvironIS([]byte{subIS, 99, 'A'}); len(got) != 0 {
		t.Errorf("invalid marker should abort, got %v", got)
	}
	// Escaped byte in the name and in the value.
	nameEsc := []byte{subIS, envVar, envEsc, 'K', envValue, envEsc, 'V'}
	got := parseNewEnvironIS(nameEsc)
	if got["K"] != "V" {
		t.Errorf("escaped name/value = %v", got)
	}
	// Empty name is skipped (VALUE marker immediately after VAR).
	if got := parseNewEnvironIS([]byte{subIS, envVar, envValue, '1'}); len(got) != 0 {
		t.Errorf("empty name should be skipped, got %v", got)
	}
}
