//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package bridge

import (
	"errors"
	"testing"
)

// Ported from packages/provide-uterm/tests/test_protocol_negotiation.py.

func TestNegotiateProtocolVersion(t *testing.T) {
	cases := []struct {
		name      string
		clientMin int
		clientMax int
		want      int
		wantOK    bool
	}{
		{"identical range returns max", 1, 1, 1, true},
		{"overlap picks highest", 1, 5, 1, true},
		{"client min above server max", 2, 5, 0, false},
		{"client max below server min", 0, 0, 0, false},
		{"no overlap", 99, 100, 0, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := NegotiateProtocolVersion(tc.clientMin, tc.clientMax)
			if got != tc.want || ok != tc.wantOK {
				t.Fatalf("NegotiateProtocolVersion(%d,%d) = (%d,%v), want (%d,%v)",
					tc.clientMin, tc.clientMax, got, ok, tc.want, tc.wantOK)
			}
		})
	}
}

func TestConstantsAreConsistent(t *testing.T) {
	if MinProtocolVersion > PreferredProtocolVersion || PreferredProtocolVersion > MaxProtocolVersion {
		t.Fatalf("inconsistent constants: min=%d preferred=%d max=%d",
			MinProtocolVersion, PreferredProtocolVersion, MaxProtocolVersion)
	}
	if CurrentProtocolVersion != PreferredProtocolVersion {
		t.Fatalf("CurrentProtocolVersion=%d, want %d", CurrentProtocolVersion, PreferredProtocolVersion)
	}
}

func TestNegotiateSuccess(t *testing.T) {
	v, err := Negotiate(1, 1)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if v != 1 {
		t.Fatalf("Negotiate(1,1) = %d, want 1", v)
	}
}

func TestNegotiateMismatchError(t *testing.T) {
	v, err := Negotiate(99, 100)
	if v != 0 {
		t.Fatalf("expected selected 0 on mismatch, got %d", v)
	}
	var mismatch *ProtocolMismatchError
	if !errors.As(err, &mismatch) {
		t.Fatalf("expected *ProtocolMismatchError, got %T: %v", err, err)
	}
	if mismatch.ClientMin != 99 || mismatch.ClientMax != 100 {
		t.Fatalf("client bounds wrong: %+v", mismatch)
	}
	if mismatch.ServerMin != MinProtocolVersion || mismatch.ServerMax != MaxProtocolVersion {
		t.Fatalf("server bounds wrong: %+v", mismatch)
	}
	if mismatch.Error() == "" {
		t.Fatal("expected a non-empty error message")
	}
}

func TestParseClientRange(t *testing.T) {
	cases := []struct {
		name    string
		msg     map[string]any
		wantMin int
		wantMax int
	}{
		{
			name:    "protocol block",
			msg:     map[string]any{"protocol": map[string]any{"min": float64(1), "max": float64(1), "preferred": float64(1)}},
			wantMin: 1, wantMax: 1,
		},
		{
			name:    "protocol block mismatch bounds",
			msg:     map[string]any{"protocol": map[string]any{"min": float64(99), "max": float64(100)}},
			wantMin: 99, wantMax: 100,
		},
		{
			name:    "protocol block missing fields defaults",
			msg:     map[string]any{"protocol": map[string]any{}},
			wantMin: MinProtocolVersion, wantMax: MaxProtocolVersion,
		},
		{
			name:    "legacy protocol_version",
			msg:     map[string]any{"protocol_version": float64(1)},
			wantMin: 1, wantMax: 1,
		},
		{
			name:    "legacy protocol_version below one floors to one",
			msg:     map[string]any{"protocol_version": float64(0)},
			wantMin: 1, wantMax: 1,
		},
		{
			name:    "no protocol field defaults to one",
			msg:     map[string]any{"input_mode": "open"},
			wantMin: 1, wantMax: 1,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			gotMin, gotMax := ParseClientRange(tc.msg)
			if gotMin != tc.wantMin || gotMax != tc.wantMax {
				t.Fatalf("ParseClientRange = (%d,%d), want (%d,%d)", gotMin, gotMax, tc.wantMin, tc.wantMax)
			}
		})
	}
}

func TestNegotiateFromHello(t *testing.T) {
	// Accepted: legacy int, protocol block, and missing (default {1,1}).
	for _, msg := range []map[string]any{
		{"type": "worker_hello", "input_mode": "open", "protocol_version": float64(1)},
		{"type": "worker_hello", "input_mode": "hijack", "protocol": map[string]any{"min": float64(1), "max": float64(1), "preferred": float64(1)}},
		{"type": "worker_hello", "input_mode": "open"},
	} {
		v, err := NegotiateFromHello(msg)
		if err != nil {
			t.Fatalf("NegotiateFromHello(%v) error: %v", msg, err)
		}
		if v != MaxProtocolVersion {
			t.Fatalf("NegotiateFromHello(%v) = %d, want %d", msg, v, MaxProtocolVersion)
		}
	}

	// Rejected: no overlap yields a mismatch error carrying the bounds.
	mismatchMsg := map[string]any{
		"type":       "worker_hello",
		"input_mode": "open",
		"protocol":   map[string]any{"min": float64(99), "max": float64(100), "preferred": float64(99)},
	}
	_, err := NegotiateFromHello(mismatchMsg)
	var mismatch *ProtocolMismatchError
	if !errors.As(err, &mismatch) {
		t.Fatalf("expected mismatch error, got %v", err)
	}
	if mismatch.ClientMin != 99 || mismatch.ClientMax != 100 {
		t.Fatalf("bounds wrong: %+v", mismatch)
	}
}
