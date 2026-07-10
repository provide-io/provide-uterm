//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"encoding/json"
	"testing"
)

// TestRunWatchBareIDNoServer covers the validation path before the TUI starts.
func TestRunWatchBareIDNoServer(t *testing.T) {
	if err := runWatch(context.Background(), "bare-id", "", "horizontal", "", ""); err == nil {
		t.Fatal("bare id without --server should error before launching the TUI")
	}
}

// TestFrameCoercionsJSONNumber covers the json.Number branches (frames decoded
// with encoding/json use float64, but a UseNumber-style decode yields Numbers).
func TestFrameCoercionsJSONNumber(t *testing.T) {
	m := map[string]any{
		"i":  json.Number("42"),
		"f":  json.Number("1.5"),
		"s":  json.Number("7"),
		"fl": float64(3),
	}
	if got := frameInt(m, "i"); got != 42 {
		t.Errorf("frameInt = %d", got)
	}
	if got := frameFloat(m, "f"); got != 1.5 {
		t.Errorf("frameFloat = %v", got)
	}
	if got := frameStr(m, "s"); got != "7" {
		t.Errorf("frameStr(Number) = %q", got)
	}
	if got := frameStr(m, "fl"); got != "3" {
		t.Errorf("frameStr(float) = %q", got)
	}
	if got := frameInt(m, "missing"); got != 0 {
		t.Errorf("missing int = %d", got)
	}
	if got := frameFloat(m, "missing"); got != 0 {
		t.Errorf("missing float = %v", got)
	}
}

// TestFrameHeadersCoercion covers header map extraction incl. non-string values.
func TestFrameHeadersCoercion(t *testing.T) {
	h := frameHeaders(map[string]any{"A": "1", "B": 2})
	if h["A"] != "1" || h["B"] != "" {
		t.Errorf("headers = %v", h)
	}
	if got := frameHeaders("not a map"); len(got) != 0 {
		t.Errorf("non-map headers = %v", got)
	}
}

// TestNewWatchModelDefaultsLayout normalizes an unknown layout to horizontal.
func TestNewWatchModelDefaultsLayout(t *testing.T) {
	if m := newWatchModel("t", "bogus"); m.layoutMode != "horizontal" {
		t.Errorf("layout = %q", m.layoutMode)
	}
}
