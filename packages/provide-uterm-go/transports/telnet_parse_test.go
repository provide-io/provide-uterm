//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

import (
	"bytes"
	"testing"
)

// TestParseTelnetBuffer is a table-driven port of the Python parser tests,
// covering every branch of parseTelnetBuffer / findSubnegEnd.
func TestParseTelnetBuffer(t *testing.T) {
	tests := []struct {
		name         string
		input        []byte
		final        bool
		wantPayload  []byte
		wantConsumed int
		wantEvents   []telnetEvent
	}{
		{
			name:         "plain data no iac",
			input:        []byte("hello"),
			wantPayload:  []byte("hello"),
			wantConsumed: 5,
		},
		{
			name:         "empty buffer",
			input:        []byte{},
			wantPayload:  []byte{},
			wantConsumed: 0,
		},
		{
			name:         "iac iac unescape to single 0xff",
			input:        []byte{'A', iacByte, iacByte, 'B'},
			wantPayload:  []byte{'A', iacByte, 'B'},
			wantConsumed: 4,
		},
		{
			name:         "negotiate DO event consumed",
			input:        []byte{iacByte, cmdDO, optBIN},
			wantPayload:  []byte{},
			wantConsumed: 3,
			wantEvents:   []telnetEvent{{kind: evNegotiate, cmd: cmdDO, opt: optBIN}},
		},
		{
			name:         "negotiate WILL then data",
			input:        []byte{iacByte, cmdWILL, optECHO, 'X'},
			wantPayload:  []byte{'X'},
			wantConsumed: 4,
			wantEvents:   []telnetEvent{{kind: evNegotiate, cmd: cmdWILL, opt: optECHO}},
		},
		{
			name:         "subnegotiation payload extracted",
			input:        []byte{iacByte, cmdSB, optTTYPE, 1, iacByte, cmdSE},
			wantPayload:  []byte{},
			wantConsumed: 6,
			wantEvents:   []telnetEvent{{kind: evSubneg, payload: []byte{optTTYPE, 1}}},
		},
		{
			name:         "unknown command skipped two bytes",
			input:        []byte{iacByte, 200, 'Y'},
			wantPayload:  []byte{'Y'},
			wantConsumed: 3,
		},
		{
			name:         "trailing lone iac not final leaves unconsumed",
			input:        []byte{'A', iacByte},
			wantPayload:  []byte{'A'},
			wantConsumed: 1,
		},
		{
			name:         "trailing lone iac final emitted literal",
			input:        []byte{'A', iacByte},
			final:        true,
			wantPayload:  []byte{'A', iacByte},
			wantConsumed: 2,
		},
		{
			name:         "truncated negotiation not final leaves unconsumed",
			input:        []byte{'A', iacByte, cmdDO},
			wantPayload:  []byte{'A'},
			wantConsumed: 1,
		},
		{
			name:         "truncated negotiation final emitted literal",
			input:        []byte{'A', iacByte, cmdDO},
			final:        true,
			wantPayload:  []byte{'A', iacByte, cmdDO},
			wantConsumed: 3,
		},
		{
			name:         "truncated subneg not final leaves unconsumed",
			input:        []byte{'A', iacByte, cmdSB, optTTYPE, 'h', 'i'},
			wantPayload:  []byte{'A'},
			wantConsumed: 1,
		},
		{
			name:         "truncated subneg final emitted literal",
			input:        []byte{'A', iacByte, cmdSB, optTTYPE, 'h', 'i'},
			final:        true,
			wantPayload:  []byte{'A', iacByte, cmdSB, optTTYPE, 'h', 'i'},
			wantConsumed: 6,
		},
		{
			name:         "lone iac at start not final",
			input:        []byte{iacByte},
			wantPayload:  []byte{},
			wantConsumed: 0,
		},
		{
			name:         "mixed data and negotiation",
			input:        []byte{'h', iacByte, cmdWONT, optNAWS, 'i', iacByte, iacByte},
			wantPayload:  []byte{'h', 'i', iacByte},
			wantConsumed: 7,
			wantEvents:   []telnetEvent{{kind: evNegotiate, cmd: cmdWONT, opt: optNAWS}},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			payload, events, consumed := parseTelnetBuffer(tc.input, tc.final)
			if !bytes.Equal(payload, tc.wantPayload) {
				t.Errorf("payload = %v, want %v", payload, tc.wantPayload)
			}
			if consumed != tc.wantConsumed {
				t.Errorf("consumed = %d, want %d", consumed, tc.wantConsumed)
			}
			if len(events) != len(tc.wantEvents) {
				t.Fatalf("events len = %d, want %d (%+v)", len(events), len(tc.wantEvents), events)
			}
			for i, ev := range events {
				want := tc.wantEvents[i]
				if ev.kind != want.kind || ev.cmd != want.cmd || ev.opt != want.opt || !bytes.Equal(ev.payload, want.payload) {
					t.Errorf("event[%d] = %+v, want %+v", i, ev, want)
				}
			}
		})
	}
}

func TestFindSubnegEnd(t *testing.T) {
	// Complete SB..SE block.
	buf := []byte{cmdSB, optTTYPE, 1, iacByte, cmdSE}
	if end, ok := findSubnegEnd(buf, 1); !ok || end != 5 {
		t.Errorf("findSubnegEnd complete = (%d,%v), want (5,true)", end, ok)
	}
	// Incomplete (no SE).
	if end, ok := findSubnegEnd([]byte{optTTYPE, 1, 2, 3}, 0); ok {
		t.Errorf("findSubnegEnd incomplete = (%d,%v), want (_,false)", end, ok)
	}
	// IAC without SE (IAC at very end).
	if _, ok := findSubnegEnd([]byte{1, iacByte}, 0); ok {
		t.Errorf("findSubnegEnd trailing IAC should be incomplete")
	}
}

func TestEscapeHelpers(t *testing.T) {
	// escapeOutgoing: DEL->BS, IAC doubled, plain unchanged.
	got := escapeOutgoing([]byte{0x7f, iacByte, 'a'})
	want := []byte{0x08, iacByte, iacByte, 'a'}
	if !bytes.Equal(got, want) {
		t.Errorf("escapeOutgoing = %v, want %v", got, want)
	}
	// escapeIAC: only 0xFF doubled.
	if g := escapeIAC([]byte{iacByte, 5, iacByte}); !bytes.Equal(g, []byte{iacByte, iacByte, 5, iacByte, iacByte}) {
		t.Errorf("escapeIAC = %v", g)
	}
}

func TestBuildSequenceHelpers(t *testing.T) {
	cases := []struct {
		got  []byte
		want []byte
	}{
		{BuildWill(TelnetECHO), []byte{iacByte, cmdWILL, optECHO}},
		{BuildWont(TelnetNAWS), []byte{iacByte, cmdWONT, optNAWS}},
		{BuildDo(TelnetSGA), []byte{iacByte, cmdDO, optSGA}},
		{BuildDont(TelnetBINARY), []byte{iacByte, cmdDONT, optBIN}},
	}
	for i, c := range cases {
		if !bytes.Equal(c.got, c.want) {
			t.Errorf("case %d = %v, want %v", i, c.got, c.want)
		}
	}
}
