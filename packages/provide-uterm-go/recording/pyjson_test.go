//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package recording

import "testing"

// PyJSONSize is len(json.dumps(v)) under CPython's defaults. The expected
// lengths were read off CPython 3.
func TestPyJSONSizeMatchesCPython(t *testing.T) {
	cases := []struct {
		v    any
		want int
	}{
		{map[string]any{"a": []any{1, 2}, "b": "é"}, 28},
		{map[string]any{}, 2},
		{[]any{}, 2},
		{"x", 3},
		{map[string]any{
			"s": "☃ <&> \x7f \n 😀",
			"f": 1791443079.2029932,
			"i": -3,
			"n": nil,
			"t": true,
			"e": []any{map[string]any{}, []any{}},
		}, 113},
		// Typed Go values a record carries are measured as the JSON they become.
		{map[string]string{"worker": "w1", "n": "2"}, 26},
	}
	for _, c := range cases {
		if got, err := PyJSONSize(c.v); err != nil || got != c.want {
			t.Errorf("PyJSONSize(%v) = %d, want %d", c.v, got, c.want)
		}
	}
}

// A value JSON cannot carry has no size, as json.dumps raises on one.
func TestPyJSONSizeOfAnUnencodableValue(t *testing.T) {
	if _, err := PyJSONSize(map[string]any{"c": make(chan int)}); err == nil {
		t.Fatal("PyJSONSize(chan) did not fail")
	}
}

// The in-memory store reports size_bytes as the reference's does: the sum of
// len(json.dumps(event)) + 1 over its events.
func TestInMemoryMetaSizeIsCPythonSized(t *testing.T) {
	s := NewInMemoryStore()
	_ = s.AppendEvents("s1", []Event{{"ts": 1.5, "event": "e", "data": map[string]any{"v": []any{0, 0}}}})
	meta, err := s.RecordingMeta("s1")
	if err != nil {
		t.Fatal(err)
	}
	// len('{"ts": 1.5, "event": "e", "data": {"v": [0, 0]}}') + 1
	if meta.SizeBytes != 49 {
		t.Fatalf("size_bytes = %d, want 49", meta.SizeBytes)
	}
}
