//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"strings"
	"testing"

	"github.com/charmbracelet/lipgloss"
	"github.com/muesli/termenv"
)

// TestWatchViewGolden pins the horizontal-layout render for a known model state.
// Styling is forced to the ASCII profile so the golden is plain, deterministic
// text.
func TestWatchViewGolden(t *testing.T) {
	lipgloss.SetColorProfile(termenv.Ascii) // strip ANSI styling for the golden

	m := newWatchModel("tunXYZ", "horizontal")
	m.connected = true
	st := 200
	dur := 12.0
	m.exchanges = []*exchange{
		{reqID: "1", method: "GET", url: "/api/health", status: &st, statusText: "OK", durationMs: &dur, resBodySize: 2048,
			reqHeaders: map[string]string{"Host": "x"}, resHeaders: map[string]string{"Content-Type": "text/plain"}},
		{reqID: "2", method: "POST", url: "/api/data"},
	}

	got := m.View()
	want := strings.Join([]string{
		"uterm watch — tunXYZ",
		"  Method  URL                                      Status Dur      Size    ",
		"> GET     /api/health                              200    12ms     2.0KB   ",
		"  POST    /api/data                                ...    -        -       ",
		"",
		"GET /api/health",
		"200 OK — 12ms",
		"",
		"Request Headers",
		"  Host: x",
		"",
		"Response Headers",
		"  Content-Type: text/plain",
		" tunXYZ  Connected  2 requests  layout=horizontal  filter=ALL  [l]ayout [f]ilter [q]uit",
	}, "\n")
	if got != want {
		t.Errorf("golden mismatch:\n--- got ---\n%q\n--- want ---\n%q", got, want)
	}
}

func TestWatchViewEmpty(t *testing.T) {
	lipgloss.SetColorProfile(termenv.Ascii)
	m := newWatchModel("t", "horizontal")
	v := m.View()
	if !strings.Contains(v, "(no requests)") || !strings.Contains(v, "Disconnected") {
		t.Errorf("empty view:\n%s", v)
	}
}

func TestWatchViewQuitting(t *testing.T) {
	m := newWatchModel("t", "horizontal")
	m.quitting = true
	if m.View() != "" {
		t.Error("quitting view should be empty")
	}
}

func TestTruncate(t *testing.T) {
	if got := truncate("short", 40); got != "short" {
		t.Errorf("got %q", got)
	}
	if got := truncate("abcdef", 4); got != "abc…" {
		t.Errorf("got %q", got)
	}
	if got := truncate("ab", 1); got != "a" {
		t.Errorf("got %q", got)
	}
}
