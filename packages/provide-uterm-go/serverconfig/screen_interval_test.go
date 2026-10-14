//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import (
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"testing"
)

const screenIntervalRefusal = "recording.screen_interval_s must be 0 (only screens sent to a viewer) " +
	"or a finite number of seconds >= 0.25, got: "

// recording.screen_interval_s is off (0) by default and is read from
// [recording].
func TestTheScreenIntervalIsOffByDefault(t *testing.T) {
	if got := DefaultServerConfig().Recording.ScreenIntervalS; got != 0 {
		t.Fatalf("screen_interval_s defaults to %v, want 0", got)
	}
	for _, v := range []float64{0, 0.25, 0.5, 1e300} {
		cfg, err := ConfigFromMapping(map[string]any{"recording": map[string]any{"screen_interval_s": v}})
		if err != nil {
			t.Fatalf("screen_interval_s = %v refused: %v", v, err)
		}
		if cfg.Recording.ScreenIntervalS != v {
			t.Fatalf("screen_interval_s = %v, want %v", cfg.Recording.ScreenIntervalS, v)
		}
	}
}

// An interval that could not mean what it says is refused with the
// reference's text, the value printed as Python prints a float: nan passed a
// `< 0` test and then failed every comparison the recorder makes, inf records
// one screen and never another, and anything under the settle period fills a
// quota in minutes.
func TestAScreenIntervalThatCouldNotMeanWhatItSaysIsRefused(t *testing.T) {
	for _, tc := range []struct {
		value any
		got   string
	}{
		{-1, "-1.0"},
		{-1.0, "-1.0"},
		{0.1, "0.1"},
		{0.125, "0.125"},
		{math.NaN(), "nan"},
		{math.Inf(1), "inf"},
		{math.Inf(-1), "-inf"},
		{-1234567, "-1234567.0"},
		{1e-5, "1e-05"},
		{-1e20, "-1e+20"},
	} {
		_, err := ConfigFromMapping(map[string]any{"recording": map[string]any{"screen_interval_s": tc.value}})
		if want := screenIntervalRefusal + tc.got; err == nil || err.Error() != want {
			t.Errorf("screen_interval_s = %v: err = %v, want %q", tc.value, err, want)
		}
		if f, ok := tc.value.(float64); ok {
			if err := validateScreenInterval(f); err == nil || err.Error() != screenIntervalRefusal+tc.got {
				t.Errorf("validateScreenInterval(%v) = %v", f, err)
			}
		}
	}
}

// TOML's own nan and inf literals reach the same refusal, not a JSON encoder
// error from decoding the section.
func TestNonFiniteScreenIntervalLiteralsInTOMLAreRefused(t *testing.T) {
	for literal, got := range map[string]string{"nan": "nan", "inf": "inf", "+inf": "inf", "-inf": "-inf"} {
		path := filepath.Join(t.TempDir(), "server.toml")
		if err := os.WriteFile(path, []byte("[recording]\nscreen_interval_s = "+literal+"\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		_, err := LoadServerConfig(path)
		if want := screenIntervalRefusal + got; err == nil || err.Error() != want {
			t.Errorf("%s: err = %v, want %q", literal, err, want)
		}
	}
}

// The RecordingConfig cases of the shared corpus (gen_serverconfig_golden.py)
// are accepted or refused, with the reference's text, as Python does.
func TestRecordingConfigMatchesReferenceCorpus(t *testing.T) {
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
		if c.Model != "RecordingConfig" {
			continue
		}
		seen++
		_, err := ConfigFromMapping(map[string]any{"recording": c.Kwargs})
		switch {
		case c.Error == nil && err != nil:
			t.Errorf("%s: refused: %v", c.Name, err)
		case c.Error != nil && (err == nil || err.Error() != *c.Error):
			t.Errorf("%s: err = %v, want %q", c.Name, err, *c.Error)
		}
	}
	if seen == 0 {
		t.Fatal("the corpus recorded no RecordingConfig cases")
	}
}
