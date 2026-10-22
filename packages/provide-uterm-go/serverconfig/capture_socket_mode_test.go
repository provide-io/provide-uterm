//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const (
	captureModeNotAnInteger = "pam.capture_socket_mode must be an integer: 0o600 or 0o660"
	captureModeRefused      = "pam.capture_socket_mode must be 0o600 or 0o660, got: "
)

// The mode is owner-only by default; owner-only and owner-and-group are read
// from a mapping and from TOML written in octal.
func TestCaptureSocketModeDefaultsAndIsRead(t *testing.T) {
	if got := DefaultServerConfig().Pam.CaptureSocketMode; got != 0o600 {
		t.Fatalf("default = %o", got)
	}
	for _, mode := range []any{int64(0o600), int64(0o660), 0o660} {
		cfg, err := ConfigFromMapping(map[string]any{"pam": map[string]any{"capture_socket_mode": mode}})
		if err != nil {
			t.Fatal(err)
		}
		if cfg.Pam.CaptureSocketMode != 0o600 && cfg.Pam.CaptureSocketMode != 0o660 {
			t.Fatalf("%v read as %o", mode, cfg.Pam.CaptureSocketMode)
		}
	}
	path := filepath.Join(t.TempDir(), "server.toml")
	if err := os.WriteFile(path, []byte("[pam]\ncapture_socket_mode = 0o660\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := LoadServerConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Pam.CaptureSocketMode != 0o660 {
		t.Fatalf("loaded %o", cfg.Pam.CaptureSocketMode)
	}
}

// Any other mode is refused, quoted as Python's oct() writes it.
func TestCaptureSocketModeRefusesAnyOtherMode(t *testing.T) {
	for _, tc := range []struct {
		mode  any
		shown string
	}{
		{int64(0o666), "0o666"}, {int64(0o664), "0o664"}, {int64(0o640), "0o640"}, {int64(0o700), "0o700"},
		{int64(0o770), "0o770"}, {int64(0o2660), "0o2660"}, {int64(0), "0o0"}, {int64(-1), "-0o1"},
		{-0o600, "-0o600"},
	} {
		_, err := ConfigFromMapping(map[string]any{"pam": map[string]any{"capture_socket_mode": tc.mode}})
		if err == nil || err.Error() != captureModeRefused+tc.shown {
			t.Fatalf("%v: err = %v, want %q", tc.mode, err, captureModeRefused+tc.shown)
		}
	}
}

// A value that is not an integer is refused before it is decoded, so neither
// a string nor a float nor a bool is read as a number.
func TestCaptureSocketModeRefusesWhatIsNotAnInteger(t *testing.T) {
	for _, mode := range []any{"0o660", "432", 432.0, true, nil, []any{int64(432)}} {
		_, err := ConfigFromMapping(map[string]any{"pam": map[string]any{"capture_socket_mode": mode}})
		if err == nil || err.Error() != captureModeNotAnInteger {
			t.Fatalf("%#v: err = %v, want %q", mode, err, captureModeNotAnInteger)
		}
	}
}

// fromJSONNumbers is v with each json.Number read as the reference wrote it:
// an int64 when it has no fraction or exponent (TOML's integer), a float64
// otherwise (TOML's float). Plain JSON decoding would make 432 and 432.0 the
// same float64, and the type check tells them apart on purpose.
func fromJSONNumbers(t *testing.T, v any) any {
	t.Helper()
	switch x := v.(type) {
	case json.Number:
		if n, err := x.Int64(); err == nil && !strings.ContainsAny(x.String(), ".eE") {
			return n
		}
		f, err := x.Float64()
		if err != nil {
			t.Fatal(err)
		}
		return f
	case []any:
		out := make([]any, len(x))
		for i, item := range x {
			out[i] = fromJSONNumbers(t, item)
		}
		return out
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, item := range x {
			out[k] = fromJSONNumbers(t, item)
		}
		return out
	default:
		return v
	}
}

// The PamConfig cases of the shared corpus (gen_serverconfig_golden.py) are
// accepted or refused, with the reference's text, as Python does.
func TestPamConfigMatchesReferenceCorpus(t *testing.T) {
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
	decoder := json.NewDecoder(bytes.NewReader(blob))
	decoder.UseNumber()
	if err := decoder.Decode(&golden); err != nil {
		t.Fatal(err)
	}
	seen := 0
	for _, c := range golden.Models {
		if c.Model != "PamConfig" {
			continue
		}
		if _, ok := c.Kwargs["capture_socket_mode"]; ok {
			seen++
		}
		_, err := ConfigFromMapping(map[string]any{"pam": fromJSONNumbers(t, c.Kwargs)})
		switch {
		case c.Error == nil && err != nil:
			t.Errorf("%s: refused: %v", c.Name, err)
		case c.Error != nil && (err == nil || err.Error() != *c.Error):
			t.Errorf("%s: err = %v, want %q", c.Name, err, *c.Error)
		}
	}
	if seen == 0 {
		t.Fatal("the corpus recorded no capture_socket_mode cases")
	}
}
