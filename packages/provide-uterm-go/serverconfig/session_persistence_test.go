//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// [session_persistence] is off by default, with a thousand rows of
// scrollback, and every key is read from the section.
func TestSessionPersistenceDefaultsAndKeys(t *testing.T) {
	got := DefaultServerConfig().SessionPersistence
	if got != (SessionPersistenceConfig{PreviousScrollbackLines: 1000}) {
		t.Fatalf("defaults = %+v", got)
	}
	path := filepath.Join(t.TempDir(), "server.toml")
	body := "[session_persistence]\npersist_capture = true\nprevious_keep_days = 7\nprevious_scrollback_lines = 50\n" +
		"state_dir = \"/var/lib/uterm//capture/\"\n"
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := LoadServerConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	dir := "/var/lib/uterm/capture"
	want := SessionPersistenceConfig{PersistCapture: true, PreviousKeepDays: 7, PreviousScrollbackLines: 50, StateDir: &dir}
	if got := cfg.SessionPersistence; got.StateDir == nil || *got.StateDir != dir || (got != SessionPersistenceConfig{
		PersistCapture: true, PreviousKeepDays: 7, PreviousScrollbackLines: 50, StateDir: got.StateDir,
	}) {
		t.Fatalf("loaded %+v, want %+v", cfg.SessionPersistence, want)
	}
}

// A bound outside its range is refused in the reference's words; the bounds
// themselves are kept.
func TestSessionPersistenceBounds(t *testing.T) {
	const days = "session_persistence.previous_keep_days must be between 0 (keep no previous sessions) and 3650, got: "
	const lines = "session_persistence.previous_scrollback_lines must be between 0 and 10000, got: "
	for _, tc := range []struct {
		section map[string]any
		want    string
	}{
		{map[string]any{"previous_keep_days": -1}, days + "-1"},
		{map[string]any{"previous_keep_days": 3651}, days + "3651"},
		{map[string]any{"previous_scrollback_lines": -1}, lines + "-1"},
		{map[string]any{"previous_scrollback_lines": 10001}, lines + "10001"},
		{map[string]any{"previous_keep_days": 3650, "previous_scrollback_lines": 10000, "state_dir": "/s"}, ""},
		{map[string]any{"previous_keep_days": 0, "previous_scrollback_lines": 0}, ""},
		{map[string]any{"persist_captures": true}, "Extra inputs are not permitted"},
	} {
		_, err := ConfigFromMapping(map[string]any{"session_persistence": tc.section})
		switch {
		case tc.want == "" && err != nil:
			t.Errorf("%v refused: %v", tc.section, err)
		case tc.want != "" && (err == nil || err.Error() != tc.want):
			t.Errorf("%v: err = %v, want %q", tc.section, err, tc.want)
		}
	}
}

// state_dir is named outright whenever a feature is on, and is absolute;
// both refusals are in the reference's words.
func TestTheStateDirectoryIsRequiredAndAbsolute(t *testing.T) {
	const required = "session_persistence.state_dir is required when session_persistence.persist_capture " +
		"is on or session_persistence.previous_keep_days is above 0"
	for _, tc := range []struct {
		section map[string]any
		want    string
	}{
		{map[string]any{"persist_capture": true}, required},
		{map[string]any{"previous_keep_days": 1}, required},
		{map[string]any{"state_dir": "relative//dir/"}, "session_persistence.state_dir must be an absolute path, got: relative/dir"},
		{map[string]any{"state_dir": ""}, "session_persistence.state_dir must be an absolute path, got: ."},
		{map[string]any{"state_dir": "/s"}, ""},
		{map[string]any{"persist_capture": true, "state_dir": "/s"}, ""},
	} {
		_, err := ConfigFromMapping(map[string]any{"session_persistence": tc.section})
		switch {
		case tc.want == "" && err != nil:
			t.Errorf("%v refused: %v", tc.section, err)
		case tc.want != "" && (err == nil || err.Error() != tc.want):
			t.Errorf("%v: err = %v, want %q", tc.section, err, tc.want)
		}
	}
}

// The SessionPersistenceConfig cases of the shared corpus
// (gen_serverconfig_golden.py) are accepted or refused, with the reference's
// text, as Python does.
func TestSessionPersistenceMatchesReferenceCorpus(t *testing.T) {
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	var blob []byte
	for {
		blob, err = os.ReadFile(filepath.Join(dir, "packages", "provide-uterm-ts", "testdata", "serverconfig_golden.json"))
		if err == nil {
			break
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("serverconfig_golden.json not found above cwd")
		}
		dir = parent
	}
	var golden struct {
		Models []struct {
			Name   string         `json:"name"`
			Model  string         `json:"model"`
			Kwargs map[string]any `json:"kwargs"`
			Error  *string        `json:"error"`
		} `json:"models"`
	}
	if err := json.Unmarshal(blob, &golden); err != nil {
		t.Fatal(err)
	}
	seen := 0
	for _, c := range golden.Models {
		if c.Model != "SessionPersistenceConfig" {
			continue
		}
		seen++
		_, err := ConfigFromMapping(map[string]any{"session_persistence": c.Kwargs})
		switch {
		case c.Error == nil && err != nil:
			t.Errorf("%s: refused: %v", c.Name, err)
		case c.Error != nil && (err == nil || err.Error() != *c.Error):
			t.Errorf("%s: err = %v, want %q", c.Name, err, *c.Error)
		}
	}
	if seen == 0 {
		t.Fatal("the corpus recorded no SessionPersistenceConfig cases")
	}
}
