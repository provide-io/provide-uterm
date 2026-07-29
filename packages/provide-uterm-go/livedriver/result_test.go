//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package livedriver

import (
	"bytes"
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

func TestDecodeBody(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		want string // the value re-encoded, so shape and number form both show
	}{
		{"object", `{"status":"ok"}`, `{"status":"ok"}`},
		{"array", `[1,2]`, `[1,2]`},
		{"null", `null`, `null`},
		{"integer stays integer", `{"n":200}`, `{"n":200}`},
		{"float keeps its form", `{"n":1.50}`, `{"n":1.50}`},
		{"html not escaped", `{"s":"<b>"}`, `{"s":"<b>"}`},
		{"not json", `<!doctype html>`, `"<non-json>"`},
		{"empty", ``, `"<non-json>"`},
		// net/http's own 404 page. A streaming decode would take the leading
		// 404 and drop the rest, recording a number where there was prose.
		{"leading token then prose", "404 page not found\n", `"<non-json>"`},
		{"json then trailing junk", `{"a":1} oops`, `"<non-json>"`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var buf bytes.Buffer
			if err := WriteLine(&buf, decodeBody([]byte(tc.raw))); err != nil {
				t.Fatalf("WriteLine: %v", err)
			}
			if got := strings.TrimSpace(buf.String()); got != tc.want {
				t.Fatalf("decodeBody(%q) → %s, want %s", tc.raw, got, tc.want)
			}
		})
	}
}

func TestRawFields(t *testing.T) {
	f := rawFields(200, []byte(`{"ok":true}`))
	if f.Status == nil || *f.Status != 200 || !f.OK || f.Error != nil {
		t.Fatalf("200 → %+v", f)
	}
	for _, status := range []int{199, 204, 299, 300, 401, 500} {
		f := rawFields(status, nil)
		wantOK := status >= 200 && status < 300
		if *f.Status != status || f.OK != wantOK {
			t.Fatalf("status %d → ok %v, want %v", status, f.OK, wantOK)
		}
		if f.Body != NonJSONBody {
			t.Fatalf("empty body → %v, want %q", f.Body, NonJSONBody)
		}
	}
}

func TestFailFields(t *testing.T) {
	f := failFields("connection refused")
	if f.Status != nil || f.OK || f.Body != nil {
		t.Fatalf("fail fields = %+v", f)
	}
	if f.Error == nil || *f.Error != "connection refused" {
		t.Fatalf("error = %v", f.Error)
	}
}

func TestWriteLineIsOneLine(t *testing.T) {
	var buf bytes.Buffer
	r := newResult("010_x", StatusCompleted)
	r.Steps = append(r.Steps, StepResult{ID: "a", Fields: rawFields(200, []byte(`{"s":"<ok>"}`))})
	if err := WriteLine(&buf, r); err != nil {
		t.Fatalf("WriteLine: %v", err)
	}
	out := buf.String()
	if strings.Count(out, "\n") != 1 || !strings.HasSuffix(out, "\n") {
		t.Fatalf("not exactly one trailing newline: %q", out)
	}
	if !strings.Contains(out, `"<ok>"`) {
		t.Fatalf("html was escaped rather than passed through: %s", out)
	}
	// The line must satisfy result.schema.json's required keys.
	var decoded map[string]any
	if err := json.Unmarshal([]byte(out), &decoded); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	for _, key := range []string{"scenario_id", "language", "role", "status", "steps"} {
		if _, ok := decoded[key]; !ok {
			t.Fatalf("missing required key %q in %s", key, out)
		}
	}
	if decoded["language"] != Language || decoded["role"] != RoleClient {
		t.Fatalf("language/role wrong: %s", out)
	}
}

func TestWriteLineError(t *testing.T) {
	if err := WriteLine(errWriter{}, newResult("x", StatusCompleted)); err == nil {
		t.Fatal("expected the writer's error to surface")
	}
}

func TestNewResultHasEmptyNotNullSteps(t *testing.T) {
	var buf bytes.Buffer
	if err := WriteLine(&buf, newResult("010_x", StatusUnsupported)); err != nil {
		t.Fatalf("WriteLine: %v", err)
	}
	if !strings.Contains(buf.String(), `"steps":[]`) {
		t.Fatalf("steps should serialise as [] not null: %s", buf.String())
	}
}

func TestErrorResult(t *testing.T) {
	r := errorResult("010_x", errors.New("boom"))
	if r.Status != StatusError || r.Error == nil || *r.Error != "boom" {
		t.Fatalf("errorResult = %+v", r)
	}
	if r.ScenarioID != "010_x" || r.Language != Language {
		t.Fatalf("attribution wrong: %+v", r)
	}
}

func TestCapabilitiesAreCopied(t *testing.T) {
	first := Capabilities()
	first[0] = "clobbered"
	if Capabilities()[0] == "clobbered" {
		t.Fatal("Capabilities handed out its own backing array")
	}
	want := map[string]bool{
		"hijack.rest": false, "sessions.rest": false, "http.raw": false,
		"auth.dev_token": false, "status.observed": false,
	}
	for _, c := range Capabilities() {
		if _, ok := want[c]; !ok {
			t.Fatalf("unexpected capability %q", c)
		}
		want[c] = true
	}
	for c, seen := range want {
		if !seen {
			t.Fatalf("capability %q not reported", c)
		}
	}
}

// errWriter fails every write.
type errWriter struct{}

func (errWriter) Write([]byte) (int, error) { return 0, errors.New("write failed") }
