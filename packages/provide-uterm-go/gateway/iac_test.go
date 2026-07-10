//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package gateway

import (
	"bytes"
	"testing"
)

func TestDeriveColormode(t *testing.T) {
	cases := []struct {
		term string
		env  map[string]string
		want string
	}{
		{"", map[string]string{"COLORTERM": "truecolor"}, "passthrough"},
		{"", map[string]string{"COLORTERM": "24bit"}, "passthrough"},
		{"xterm-direct", nil, "passthrough"},
		{"foo-truecolor", nil, "passthrough"},
		{"xterm-256color", nil, "256"},
		{"screen-256color", nil, "256"},
		{"xterm", nil, "16"},
		{"vt100", nil, "16"},
		{"", map[string]string{"TERM": "linux"}, "16"},
		{"fancyterm", nil, ""},
		{"", nil, ""},
	}
	for _, c := range cases {
		if got := DeriveColormode(c.term, c.env); got != c.want {
			t.Errorf("DeriveColormode(%q,%v) = %q, want %q", c.term, c.env, got, c.want)
		}
	}
}

func TestStripIAC(t *testing.T) {
	// IAC WILL TTYPE, then "hi", then IAC IP (→ Ctrl-C), then IAC IAC (→ literal 0xff).
	in := []byte{iacIAC, iacWILL, optTTYPE, 'h', 'i', iacIAC, iacIP, iacIAC, iacIAC}
	got := stripIAC(in)
	want := []byte{'h', 'i', 0x03, 0xff}
	if !bytes.Equal(got, want) {
		t.Errorf("stripIAC = %v, want %v", got, want)
	}
}

func TestStripIACSubnegAndEOF(t *testing.T) {
	in := []byte{iacIAC, iacSB, optTTYPE, subIS, 'x', iacIAC, iacSE, 'a', iacIAC, iacEOF}
	got := stripIAC(in)
	want := []byte{'a', 0x04}
	if !bytes.Equal(got, want) {
		t.Errorf("stripIAC = %v, want %v", got, want)
	}
}

func TestIacNegotiatorFullHandshake(t *testing.T) {
	neg := NewIacNegotiator()
	start := neg.StartBytes()
	if !bytes.Equal(start, []byte{iacIAC, iacDO, optTTYPE, iacIAC, iacDO, optNewEnviron}) {
		t.Fatalf("start bytes = %v", start)
	}
	if neg.Done() {
		t.Fatal("negotiator should not be done before any reply")
	}

	// Client: WILL TTYPE, WILL NEW-ENVIRON.
	reply, cleaned := neg.Feed([]byte{iacIAC, iacWILL, optTTYPE, iacIAC, iacWILL, optNewEnviron})
	if len(cleaned) != 0 {
		t.Errorf("cleaned should be empty, got %v", cleaned)
	}
	// Expect two SB SEND requests.
	wantReply := []byte{
		iacIAC, iacSB, optTTYPE, subSEND, iacIAC, iacSE,
		iacIAC, iacSB, optNewEnviron, subSEND, iacIAC, iacSE,
	}
	if !bytes.Equal(reply, wantReply) {
		t.Fatalf("reply = %v, want %v", reply, wantReply)
	}

	// Client: TTYPE IS xterm-256color.
	ttype := append([]byte{iacIAC, iacSB, optTTYPE, subIS}, []byte("XTERM-256color")...)
	ttype = append(ttype, iacIAC, iacSE)
	neg.Feed(ttype)
	if neg.Term != "xterm-256color" {
		t.Errorf("term = %q", neg.Term)
	}

	// Client: NEW-ENVIRON IS with COLORTERM=truecolor.
	env := []byte{iacIAC, iacSB, optNewEnviron, subIS, envVar}
	env = append(env, []byte("COLORTERM")...)
	env = append(env, envValue)
	env = append(env, []byte("truecolor")...)
	env = append(env, iacIAC, iacSE)
	neg.Feed(env)
	if neg.Env["COLORTERM"] != "truecolor" {
		t.Errorf("env = %v", neg.Env)
	}
	if !neg.Done() {
		t.Error("negotiator should be done after both replies")
	}
	if neg.DerivedColormode() != "passthrough" {
		t.Errorf("derived = %q, want passthrough (COLORTERM wins)", neg.DerivedColormode())
	}
}

func TestIacNegotiatorSplitCommand(t *testing.T) {
	neg := NewIacNegotiator()
	neg.StartBytes()
	// IAC WILL split across two feeds must still parse.
	r1, _ := neg.Feed([]byte{iacIAC, iacWILL})
	if len(r1) != 0 {
		t.Errorf("partial command should produce no reply yet, got %v", r1)
	}
	r2, _ := neg.Feed([]byte{optTTYPE})
	if !bytes.Equal(r2, []byte{iacIAC, iacSB, optTTYPE, subSEND, iacIAC, iacSE}) {
		t.Errorf("reply after completion = %v", r2)
	}
}

func TestIacNegotiatorSubnegOverflow(t *testing.T) {
	neg := NewIacNegotiator()
	// Open a TTYPE SB and flood it past the cap without closing, then close.
	data := []byte{iacIAC, iacSB, optTTYPE, subIS}
	data = append(data, bytes.Repeat([]byte("A"), maxSBBytes+100)...)
	data = append(data, iacIAC, iacSE)
	neg.Feed(data)
	if neg.Term != "" {
		t.Errorf("overflowed SB should be dropped, term=%q", neg.Term)
	}
}

func TestParseNewEnvironMultiple(t *testing.T) {
	payload := []byte{subIS, envVar}
	payload = append(payload, []byte("A")...)
	payload = append(payload, envValue)
	payload = append(payload, []byte("1")...)
	payload = append(payload, envUserVar)
	payload = append(payload, []byte("B")...)
	payload = append(payload, envValue)
	payload = append(payload, []byte("2")...)
	got := parseNewEnvironIS(payload)
	if got["A"] != "1" || got["B"] != "2" {
		t.Errorf("parseNewEnvironIS = %v", got)
	}
}
