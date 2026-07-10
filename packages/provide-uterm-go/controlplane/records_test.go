//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package controlplane

import (
	"database/sql/driver"
	"testing"
)

func TestNullStringValue(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name string
		in   NullString
		want driver.Value
	}{
		{"present", Str("hi"), "hi"},
		{"null", NullStr(), nil},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got, err := tc.in.Value()
			if err != nil {
				t.Fatalf("Value() error: %v", err)
			}
			if got != tc.want {
				t.Fatalf("Value() = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestNullStringScan(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name    string
		src     any
		want    NullString
		wantErr bool
	}{
		{"nil", nil, NullStr(), false},
		{"string", "x", Str("x"), false},
		{"bytes", []byte("y"), Str("y"), false},
		{"bad", 42, NullString{}, true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var n NullString
			err := n.Scan(tc.src)
			if tc.wantErr {
				if err == nil {
					t.Fatal("expected error, got nil")
				}
				return
			}
			if err != nil {
				t.Fatalf("Scan() error: %v", err)
			}
			if n != tc.want {
				t.Fatalf("Scan() = %+v, want %+v", n, tc.want)
			}
		})
	}
}

func TestNullFloatValue(t *testing.T) {
	t.Parallel()
	got, err := Float(1.5).Value()
	if err != nil || got != 1.5 {
		t.Fatalf("Value() = %v, %v", got, err)
	}
	got, err = NullFlt().Value()
	if err != nil || got != nil {
		t.Fatalf("null Value() = %v, %v", got, err)
	}
}

func TestNullFloatScan(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name    string
		src     any
		want    NullFloat
		wantErr bool
	}{
		{"nil", nil, NullFlt(), false},
		{"float64", 2.5, Float(2.5), false},
		{"int64", int64(3), Float(3), false},
		{"bad", "nope", NullFloat{}, true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var n NullFloat
			err := n.Scan(tc.src)
			if tc.wantErr {
				if err == nil {
					t.Fatal("expected error")
				}
				return
			}
			if err != nil {
				t.Fatalf("Scan() error: %v", err)
			}
			if n != tc.want {
				t.Fatalf("Scan() = %+v, want %+v", n, tc.want)
			}
		})
	}
}

// TestRecordsAreComparable asserts value equality holds for records with equal
// content but independently constructed optional fields — the property the
// memory backend's conflict detection depends on.
func TestRecordsAreComparable(t *testing.T) {
	t.Parallel()
	a := SessionRecord{SessionID: "s", Owner: Str("u"), DeletedAt: Float(1)}
	b := SessionRecord{SessionID: "s", Owner: Str("u"), DeletedAt: Float(1)}
	if a != b {
		t.Fatal("expected equal records to compare equal")
	}
	c := SessionRecord{SessionID: "s", Owner: NullStr(), DeletedAt: Float(1)}
	if a == c {
		t.Fatal("expected differing owner to compare unequal")
	}
}
