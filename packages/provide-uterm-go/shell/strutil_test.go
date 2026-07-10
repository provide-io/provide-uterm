//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"reflect"
	"testing"
)

func TestPySplit1(t *testing.T) {
	tests := []struct {
		in   string
		want []string
	}{
		{"", nil},
		{"   ", nil},
		{"one", []string{"one"}},
		{"  one  ", []string{"one"}},
		{"a b c", []string{"a", "b c"}},
		{"  a   b c ", []string{"a", "b c "}},
		{"a\tb", []string{"a", "b"}},
	}
	for _, tt := range tests {
		if got := pySplit1(tt.in); !reflect.DeepEqual(got, tt.want) {
			t.Fatalf("pySplit1(%q) = %v, want %v", tt.in, got, tt.want)
		}
	}
}

func TestPyStripAndFields(t *testing.T) {
	if got := pyStrip("  hi \t"); got != "hi" {
		t.Fatalf("pyStrip = %q", got)
	}
	if got := pyFields("  a  b\tc "); !reflect.DeepEqual(got, []string{"a", "b", "c"}) {
		t.Fatalf("pyFields = %v", got)
	}
}

func TestTruthy(t *testing.T) {
	tests := []struct {
		v    any
		want bool
	}{
		{nil, false},
		{true, true},
		{false, false},
		{"", false},
		{"x", true},
		{0, false},
		{3, true},
		{int64(0), false},
		{int64(5), true},
		{float64(0), false},
		{1.5, true},
		{struct{}{}, true},
	}
	for _, tt := range tests {
		if got := truthy(tt.v); got != tt.want {
			t.Fatalf("truthy(%#v) = %v, want %v", tt.v, got, tt.want)
		}
	}
}

func TestToFloat(t *testing.T) {
	tests := []struct {
		v    any
		want float64
		ok   bool
	}{
		{float64(2), 2, true},
		{3, 3, true},
		{"4.5", 4.5, true},
		{"nope", 0, false},
		{true, 0, false},
	}
	for _, tt := range tests {
		got, ok := toFloat(tt.v)
		if ok != tt.ok || (ok && got != tt.want) {
			t.Fatalf("toFloat(%#v) = %v,%v want %v,%v", tt.v, got, ok, tt.want, tt.ok)
		}
	}
}

func TestPyValue(t *testing.T) {
	tests := []struct {
		v    any
		want string
	}{
		{nil, "None"},
		{float64(1), "1"},
		{float64(2.5), "2.5"},
		{"str", "str"},
	}
	for _, tt := range tests {
		if got := pyValue(tt.v); got != tt.want {
			t.Fatalf("pyValue(%#v) = %q, want %q", tt.v, got, tt.want)
		}
	}
}

func TestTextResultHelpers(t *testing.T) {
	r := textResult("a", "b")
	if r.Animated != nil || len(r.Text) != 2 {
		t.Fatalf("textResult = %+v", r)
	}
	ar := animatedResult(AnimatedResult{Frames: []string{"f"}, FPS: 1, Loop: true})
	if ar.Animated == nil || !ar.Animated.Loop {
		t.Fatalf("animatedResult = %+v", ar)
	}
}
